"""Tests for the call-counting logic in ``bricklink_api.call_limit``.

These exercise the public ``CallTracker`` behaviour (record/count/remaining/
reset, persistence, corrupt-state handling, window rollover) plus the
configurable reset time that decides when the 24-hour counter resets.
"""

import datetime
import json

import pytest

from bricklink_api import call_limit
from bricklink_api.call_limit import (
    CallLimitReached,
    CallTracker,
    _parse_reset_time,
    _read_reset_time,
)


UTC = datetime.timezone.utc


@pytest.fixture
def state_path(tmp_path):
  # A writable, isolated state file so tests never touch the package's own.
  return tmp_path / "call_count.json"


@pytest.fixture
def tracker(state_path):
  return CallTracker(path=state_path)


def _freeze_window(tracker, iso_date):
  # Pin the tracker's notion of the current window so rollover is deterministic.
  tracker._window_key = lambda: iso_date


# -- record / count / remaining ---------------------------------------------

def test_count_starts_at_zero(tracker):
  assert tracker.count() == 0


def test_record_returns_new_count(tracker):
  assert tracker.record() == 1
  assert tracker.record() == 2


def test_record_accumulates(tracker):
  tracker.record()
  tracker.record()
  tracker.record()
  assert tracker.count() == 3


def test_record_with_n(tracker):
  assert tracker.record(5) == 5
  assert tracker.record(3) == 8
  assert tracker.count() == 8


def test_remaining(tracker):
  tracker.record(10)
  assert tracker.remaining() == tracker.limit - 10


def test_remaining_reaches_zero_at_limit(state_path):
  t = CallTracker(limit=10, path=state_path)
  t.record(10)
  assert t.remaining() == 0


def test_custom_limit(state_path):
  t = CallTracker(limit=100, path=state_path)
  t.record(40)
  assert t.remaining() == 60


def test_reset(tracker):
  tracker.record(25)
  tracker.reset()
  assert tracker.count() == 0


# -- daily limit lock --------------------------------------------------------

def test_record_allows_exactly_up_to_limit(state_path):
  t = CallTracker(limit=10, path=state_path)
  assert t.record(10) == 10


def test_record_raises_once_limit_reached(state_path):
  t = CallTracker(limit=10, path=state_path)
  t.record(10)
  with pytest.raises(CallLimitReached):
    t.record()


def test_blocked_call_is_not_counted(state_path):
  t = CallTracker(limit=10, path=state_path)
  t.record(8)
  with pytest.raises(CallLimitReached):
    t.record(3)  # would reach 11; refused as a whole
  assert t.count() == 8


def test_blocked_call_does_not_persist(state_path):
  t = CallTracker(limit=10, path=state_path)
  t.record(10)
  with pytest.raises(CallLimitReached):
    t.record()
  # A fresh instance reading the same file still sees the limit fully used.
  assert CallTracker(limit=10, path=state_path).remaining() == 0


def test_limit_lock_clears_after_rollover(state_path):
  t = CallTracker(limit=10, path=state_path)
  _freeze_window(t, "2026-06-27")
  t.record(10)
  with pytest.raises(CallLimitReached):
    t.record()

  _freeze_window(t, "2026-06-28")
  assert t.record() == 1


def test_module_level_record_enforces_limit(state_path, monkeypatch):
  # The shared-instance wrappers must enforce the lock too.
  monkeypatch.setattr(call_limit, "tracker", CallTracker(limit=2, path=state_path))
  call_limit.record()
  call_limit.record()
  with pytest.raises(CallLimitReached):
    call_limit.record()


# -- persistence -------------------------------------------------------------

def test_count_persists_across_instances(state_path):
  CallTracker(path=state_path).record(12)
  assert CallTracker(path=state_path).count() == 12


def test_state_file_contents(tracker, state_path):
  _freeze_window(tracker, "2026-06-27")
  tracker.record(4)
  state = json.loads(state_path.read_text())
  assert state == {"date": "2026-06-27", "count": 4}


def test_missing_file_reads_as_zero(state_path):
  assert not state_path.exists()
  assert CallTracker(path=state_path).count() == 0


def test_corrupt_file_treated_as_empty(state_path):
  state_path.write_text("this is not json {{{")
  # A corrupt file must not raise; it reads as a fresh window.
  assert CallTracker(path=state_path).count() == 0


def test_save_leaves_no_temp_files(tracker, state_path):
  tracker.record(3)
  leftovers = list(state_path.parent.glob("*.tmp"))
  assert leftovers == []


# -- window rollover ---------------------------------------------------------

def test_count_resets_on_new_window(tracker):
  _freeze_window(tracker, "2026-06-27")
  tracker.record(9)
  assert tracker.count() == 9

  _freeze_window(tracker, "2026-06-28")
  assert tracker.count() == 0


def test_record_after_rollover_starts_fresh(tracker):
  _freeze_window(tracker, "2026-06-27")
  tracker.record(9)

  _freeze_window(tracker, "2026-06-28")
  assert tracker.record() == 1


def test_no_rollover_within_same_window(tracker):
  _freeze_window(tracker, "2026-06-27")
  tracker.record(5)
  assert tracker.count() == 5
  assert tracker.count() == 5


# -- configurable reset time -------------------------------------------------

def test_window_key_uses_utc_midnight_by_default(state_path, monkeypatch):
  # With the default 00:00 reset, the window key is just the UTC calendar date.
  _patch_now(monkeypatch, datetime.datetime(2026, 1, 15, 2, 0, tzinfo=UTC))
  t = CallTracker(path=state_path, reset_time="00:00")
  assert t._window_key() == "2026-01-15"


def test_window_key_shifts_with_reset_time(state_path, monkeypatch):
  # A 06:00 reset: 05:00 UTC still belongs to the window that opened at 06:00
  # the previous day, so the window key is the previous date.
  _patch_now(monkeypatch, datetime.datetime(2026, 1, 15, 5, 0, tzinfo=UTC))
  t = CallTracker(path=state_path, reset_time="06:00")
  assert t._window_key() == "2026-01-14"


def test_window_key_rolls_at_reset_time(state_path, monkeypatch):
  # One minute apart, straddling the 06:00 reset, lands in adjacent windows.
  _patch_now(monkeypatch, datetime.datetime(2026, 1, 15, 5, 59, tzinfo=UTC))
  before = CallTracker(path=state_path, reset_time="06:00")
  assert before._window_key() == "2026-01-14"

  _patch_now(monkeypatch, datetime.datetime(2026, 1, 15, 6, 0, tzinfo=UTC))
  after = CallTracker(path=state_path, reset_time="06:00")
  assert after._window_key() == "2026-01-15"


def test_reset_time_read_from_config_json(state_path, tmp_path):
  config = tmp_path / "config.json"
  config.write_text(json.dumps({"CallLimitResetTime": "08:30"}))
  assert _read_reset_time(config) == "08:30"


def test_reset_time_defaults_when_absent(tmp_path):
  config = tmp_path / "config.json"
  config.write_text(json.dumps({"ConsumerKey": "x"}))
  assert _read_reset_time(config) == call_limit.DEFAULT_RESET_TIME


def test_reset_time_defaults_when_config_missing(tmp_path):
  assert _read_reset_time(tmp_path / "nope.json") == call_limit.DEFAULT_RESET_TIME


def test_reset_time_defaults_when_config_corrupt(tmp_path):
  config = tmp_path / "config.json"
  config.write_text("not json {{{")
  assert _read_reset_time(config) == call_limit.DEFAULT_RESET_TIME


# -- reset-time parsing ------------------------------------------------------

def test_parse_reset_time_hours_and_minutes():
  assert _parse_reset_time("06:30") == datetime.timedelta(hours=6, minutes=30)


def test_parse_reset_time_hour_only():
  assert _parse_reset_time("6") == datetime.timedelta(hours=6)


def test_parse_reset_time_midnight():
  assert _parse_reset_time("00:00") == datetime.timedelta(0)


def test_parse_reset_time_none_is_midnight():
  assert _parse_reset_time(None) == datetime.timedelta(0)


def test_parse_reset_time_passes_through_timedelta():
  td = datetime.timedelta(hours=3)
  assert _parse_reset_time(td) == td


@pytest.mark.parametrize("bad", ["24:00", "-01:00", "30:00"])
def test_parse_reset_time_rejects_out_of_range(bad):
  with pytest.raises(ValueError):
    _parse_reset_time(bad)


def _patch_now(monkeypatch, fixed):
  # Replace call_limit's datetime so datetime.now(tz) returns `fixed`.
  class _FixedDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
      return fixed.astimezone(tz)

  monkeypatch.setattr(call_limit._datetime, "datetime", _FixedDatetime)