"""Persistent daily tracking of BrickLink API call usage.

BrickLink caps API usage at a number of requests per day (5000 at the time of
writing) and resets the counter at midnight.  Neither the reset time nor its
timezone are documented; BrickLink's servers run on US Eastern time, so that is
used as the default.  BrickLink returns no remaining-quota header, so this count
is a *client-side estimate*: it will diverge from the server if the same key is
used elsewhere, from another machine, or via a different state file.

Every call is counted regardless of what it returns (see ``method.method``).
"""

import datetime as _datetime
import json as _json
import os as _os
import pathlib as _pathlib
import tempfile as _tempfile


DAILY_LIMIT = 5000


class CallLimitReached(RuntimeError):
  """Raised when recording a call would push usage past the daily limit.

  Acts as the client-side lock: once today's count reaches ``limit``, further
  calls are refused rather than silently exceeding BrickLink's quota.
  """


# -- Reset timezone ----------------------------------------------------------
# Prefer the stdlib database (Python >= 3.9); fall back to a hand-rolled US
# Eastern tzinfo so the daily rollover stays correct on 3.6 - 3.8 without an
# extra dependency.  Override ``RESET_TZ`` (or pass ``tz=`` to CallTracker) if
# you ever observe the reset happening on a different clock.

_ZERO = _datetime.timedelta(0)


def _first_sunday_on_or_after(dt):
  # Advance dt to the next Sunday (or return it unchanged if already Sunday).
  days_to_go = 6 - dt.weekday()  # Monday == 0 ... Sunday == 6
  if days_to_go:
    dt += _datetime.timedelta(days_to_go)
  return dt


class _USEastern(_datetime.tzinfo):
  """US Eastern time with the post-2007 DST rule.

  DST runs from 02:00 on the second Sunday of March to 02:00 on the first
  Sunday of November.  Only the offset matters here (to map a UTC instant to
  the right Eastern calendar date), so the ambiguous transition hours are
  resolved by naive local comparison, which is good enough for a date.
  """

  _STD = _datetime.timedelta(hours=-5)   # EST
  _DST = _datetime.timedelta(hours=-4)   # EDT

  def utcoffset(self, dt):
    # Offset from UTC: -4h during DST, -5h otherwise.
    return self._DST if self._is_dst(dt) else self._STD

  def dst(self, dt):
    # The DST correction itself: 1h during DST, 0 otherwise.
    return (self._DST - self._STD) if self._is_dst(dt) else _ZERO

  def tzname(self, dt):
    # Human-readable abbreviation for the active offset.
    return "EDT" if self._is_dst(dt) else "EST"

  def _is_dst(self, dt):
    # True when dt falls inside the US Eastern DST window for its year.
    if dt is None:
      return False
    start = _first_sunday_on_or_after(_datetime.datetime(dt.year, 3, 8, 2))
    end = _first_sunday_on_or_after(_datetime.datetime(dt.year, 11, 1, 2))
    naive = dt.replace(tzinfo=None)
    return start <= naive < end


try:
  import zoneinfo as _zoneinfo
  RESET_TZ = _zoneinfo.ZoneInfo("America/New_York")
except (ImportError, Exception):  # noqa: B014 - ZoneInfoNotFoundError, no tzdata
  RESET_TZ = _USEastern()


# State lives next to the package, matching auth.json's convention.  If the
# package is installed read-only (e.g. site-packages), point a CallTracker at a
# writable path instead.
_STATE_PATH = _pathlib.Path(__file__).parent / "call_count.json"


class CallTracker:
  """Counts API calls made today and persists the count to a JSON file.

  The stored date is the current calendar date in ``tz``.  Reading or recording
  on a later date rolls the count back to 0.
  """

  def __init__(self, *, limit=DAILY_LIMIT, path=_STATE_PATH, tz=RESET_TZ):
    # Configure the daily limit, state-file path, and reset timezone.
    self.limit = limit
    self.path = _pathlib.Path(path)
    self.tz = tz

  def _today(self) -> str:
    # Current calendar date in the reset timezone, as an ISO string.
    return _datetime.datetime.now(self.tz).date().isoformat()

  def _load(self) -> dict:
    # Read persisted state; treat a missing or corrupt file as empty.
    try:
      with self.path.open() as f:
        return _json.load(f)
    except (FileNotFoundError, ValueError):
      return {}

  def _save(self, state: dict) -> None:
    # Atomically persist state: write to a temp file, then replace.
    fd, tmp = _tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
    try:
      with _os.fdopen(fd, "w") as f:
        _json.dump(state, f)
      _os.replace(tmp, str(self.path))
    except BaseException:
      try:
        _os.unlink(tmp)
      except OSError:
        pass
      raise

  def _current(self) -> dict:
    # Loaded state, rolled over to a fresh count if the day has changed.
    state = self._load()
    today = self._today()
    if state.get("date") != today:
      state = {"date": today, "count": 0}
    return state

  def record(self, n: int = 1) -> int:
    # Add n to today's count, persist it, and return the new count.  Refuses
    # (raising CallLimitReached) if the call would take usage past the daily
    # limit, leaving the stored count untouched.
    state = self._current()
    if state["count"] + n > self.limit:
      raise CallLimitReached(
          "BrickLink daily call limit reached: "
          "{}/{} calls used today".format(state["count"], self.limit)
      )
    state["count"] += n
    self._save(state)
    return state["count"]

  def count(self) -> int:
    # Number of calls recorded so far today.
    return self._current()["count"]

  def remaining(self) -> int:
    # Calls left before the daily limit.  The record() lock keeps this from
    # going negative, though externally-tampered state could still read so.
    return self.limit - self.count()

  def reset(self) -> None:
    # Force today's count back to 0.
    self._save({"date": self._today(), "count": 0})


# Shared default instance and module-level convenience wrappers.
tracker = CallTracker()


def record(n: int = 1) -> int:
  # Record a call against the shared default tracker.
  return tracker.record(n)


def count() -> int:
  # Calls recorded so far today on the shared default tracker.
  return tracker.count()


def remaining() -> int:
  # Calls left today on the shared default tracker.
  return tracker.remaining()