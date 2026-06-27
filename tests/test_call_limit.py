"""Tests for the call-counting logic in ``bricklink_api.call_limit``.

These exercise the public ``CallTracker`` behaviour (record/count/remaining/
reset, persistence, corrupt-state handling, daily rollover) plus the internal
US Eastern timezone helpers that decide when the daily counter resets.
"""

import datetime
import json

import pytest

from bricklink_api import call_limit
from bricklink_api.call_limit import CallTracker, _USEastern, _first_sunday_on_or_after


UTC = datetime.timezone.utc


@pytest.fixture
def state_path(tmp_path):
  # A writable, isolated state file so tests never touch the package's own.
  return tmp_path / "call_count.json"


@pytest.fixture
def tracker(state_path):
  return CallTracker(path=state_path)


def _freeze_date(tracker, iso_date):
  # Pin the tracker's notion of "today" so rollover behaviour is deterministic.
  tracker._today = lambda: iso_date


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


def test_remaining_can_go_negative(tracker):
  tracker.record(tracker.limit + 7)
  assert tracker.remaining() == -7


def test_custom_limit(state_path):
  t = CallTracker(limit=100, path=state_path)
  t.record(40)
  assert t.remaining() == 60


def test_reset(tracker):
  tracker.record(25)
  tracker.reset()
  assert tracker.count() == 0


# -- persistence -------------------------------------------------------------

def test_count_persists_across_instances(state_path):
  CallTracker(path=state_path).record(12)
  assert CallTracker(path=state_path).count() == 12


def test_state_file_contents(tracker, state_path):
  _freeze_date(tracker, "2026-06-27")
  tracker.record(4)
  state = json.loads(state_path.read_text())
  assert state == {"date": "2026-06-27", "count": 4}


def test_missing_file_reads_as_zero(state_path):
  assert not state_path.exists()
  assert CallTracker(path=state_path).count() == 0


def test_corrupt_file_treated_as_empty(state_path):
  state_path.write_text("this is not json {{{")
  # A corrupt file must not raise; it reads as a fresh day.
  assert CallTracker(path=state_path).count() == 0


def test_save_leaves_no_temp_files(tracker, state_path):
  tracker.record(3)
  leftovers = list(state_path.parent.glob("*.tmp"))
  assert leftovers == []


# -- daily rollover ----------------------------------------------------------

def test_count_resets_on_new_day(tracker):
  _freeze_date(tracker, "2026-06-27")
  tracker.record(9)
  assert tracker.count() == 9

  _freeze_date(tracker, "2026-06-28")
  assert tracker.count() == 0


def test_record_after_rollover_starts_fresh(tracker):
  _freeze_date(tracker, "2026-06-27")
  tracker.record(9)

  _freeze_date(tracker, "2026-06-28")
  assert tracker.record() == 1


def test_no_rollover_within_same_day(tracker):
  _freeze_date(tracker, "2026-06-27")
  tracker.record(5)
  assert tracker.count() == 5
  assert tracker.count() == 5


def test_today_uses_configured_timezone(state_path, monkeypatch):
  # _today must report the calendar date in the tracker's tz, not local/UTC.
  fixed = datetime.datetime(2026, 1, 15, 2, 0, tzinfo=UTC)

  class _FixedDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
      return fixed.astimezone(tz)

  monkeypatch.setattr(call_limit._datetime, "datetime", _FixedDatetime)

  # 02:00 UTC is still the previous day at the -05:00 Eastern offset.
  eastern = CallTracker(path=state_path, tz=_USEastern())
  assert eastern._today() == "2026-01-14"
  utc_tracker = CallTracker(path=state_path, tz=UTC)
  assert utc_tracker._today() == "2026-01-15"


# -- US Eastern timezone helpers --------------------------------------------

def test_first_sunday_on_or_after_returns_sunday_unchanged():
  sunday = datetime.datetime(2026, 3, 8)  # a Sunday
  assert sunday.weekday() == 6
  assert _first_sunday_on_or_after(sunday) == sunday


def test_first_sunday_on_or_after_advances_to_next_sunday():
  monday = datetime.datetime(2026, 3, 2)  # a Monday
  assert _first_sunday_on_or_after(monday) == datetime.datetime(2026, 3, 8)

  saturday = datetime.datetime(2026, 3, 7)
  assert _first_sunday_on_or_after(saturday) == datetime.datetime(2026, 3, 8)


def test_eastern_standard_time_in_winter():
  tz = _USEastern()
  winter = datetime.datetime(2026, 1, 15, 12, 0)
  assert tz.utcoffset(winter) == datetime.timedelta(hours=-5)
  assert tz.dst(winter) == datetime.timedelta(0)
  assert tz.tzname(winter) == "EST"


def test_eastern_daylight_time_in_summer():
  tz = _USEastern()
  summer = datetime.datetime(2026, 7, 15, 12, 0)
  assert tz.utcoffset(summer) == datetime.timedelta(hours=-4)
  assert tz.dst(summer) == datetime.timedelta(hours=1)
  assert tz.tzname(summer) == "EDT"


@pytest.mark.parametrize("naive, is_dst", [
  (datetime.datetime(2026, 3, 8, 1, 59), False),   # just before spring-forward
  (datetime.datetime(2026, 3, 8, 2, 0), True),     # DST begins
  (datetime.datetime(2026, 11, 1, 1, 59), True),   # just before fall-back
  (datetime.datetime(2026, 11, 1, 2, 0), False),   # DST ends
])
def test_dst_transition_boundaries(naive, is_dst):
  tz = _USEastern()
  expected = datetime.timedelta(hours=-4 if is_dst else -5)
  assert tz.utcoffset(naive) == expected


def test_utcoffset_handles_none():
  # tzinfo APIs may be probed with dt=None; must fall back to standard time.
  tz = _USEastern()
  assert tz.utcoffset(None) == datetime.timedelta(hours=-5)
  assert tz.tzname(None) == "EST"


def test_eastern_offset_maps_utc_instant_to_correct_date():
  # 02:00 UTC in winter is 21:00 the previous day in Eastern.
  tz = _USEastern()
  instant = datetime.datetime(2026, 1, 15, 2, 0, tzinfo=UTC)
  assert instant.astimezone(tz).date() == datetime.date(2026, 1, 14)