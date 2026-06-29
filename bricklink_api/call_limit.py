"""Persistent daily tracking of BrickLink API call usage.

BrickLink caps API usage at a number of requests per 24-hour window (5000 at the
time of writing).  The window is *not* anchored to midnight in any fixed
timezone: BrickLink lets you choose when your 24-hour window resets, so this
module makes no assumption about it.  Instead the reset time is read from
``config.json`` (key ``CallLimitResetTime``, an ``"HH:MM"`` 24-hour time in UTC)
and the count rolls back to 0 each day when that time is crossed.

BrickLink returns no remaining-quota header, so this count is a *client-side
estimate*: it will diverge from the server if the same key is used elsewhere,
from another machine, or via a different state file.

Every call is counted regardless of what it returns (see ``method.method``).
"""

import datetime as _datetime
import json as _json
import os as _os
import pathlib as _pathlib
import tempfile as _tempfile


DAILY_LIMIT = 5000

# Default reset time (UTC) used when config.json omits ``CallLimitResetTime``.
DEFAULT_RESET_TIME = "00:00"

_UTC = _datetime.timezone.utc

# config.json and the state file both live next to the package, matching the
# existing convention.  If the package is installed read-only (e.g.
# site-packages), point a CallTracker at a writable ``path`` instead.
_CONFIG_PATH = _pathlib.Path(__file__).parent / "config.json"
_STATE_PATH = _pathlib.Path(__file__).parent / "call_count.json"


class CallLimitReached(RuntimeError):
  """Raised when recording a call would push usage past the daily limit.

  Acts as the client-side lock: once the current window's count reaches
  ``limit``, further calls are refused rather than silently exceeding
  BrickLink's quota.
  """


def _parse_reset_time(value) -> _datetime.timedelta:
  # Turn a reset time into an offset from midnight UTC.  Accepts an "HH:MM"
  # string (UTC), a timedelta (used as-is), or None (-> midnight UTC).
  if value is None:
    return _datetime.timedelta(0)
  if isinstance(value, _datetime.timedelta):
    value_ = value
  else:
    hh, _, mm = str(value).strip().partition(":")
    value_ = _datetime.timedelta(hours=int(hh), minutes=int(mm or 0))
  if not _datetime.timedelta(0) <= value_ < _datetime.timedelta(days=1):
    raise ValueError(
        "reset time must fall within a day (00:00-23:59), got {!r}".format(value)
    )
  return value_


def _read_reset_time(path=_CONFIG_PATH) -> str:
  # Read CallLimitResetTime from config.json, defaulting when absent/unreadable.
  try:
    with _pathlib.Path(path).open() as f:
      return _json.load(f).get("CallLimitResetTime", DEFAULT_RESET_TIME)
  except (FileNotFoundError, ValueError):
    return DEFAULT_RESET_TIME


class CallTracker:
  """Counts API calls made in the current window and persists the count.

  The window is a fixed 24-hour span resetting daily at ``reset_time`` (UTC).
  The stored key is the calendar date of the window's start; reading or
  recording in a later window rolls the count back to 0.
  """

  def __init__(self, *, limit=DAILY_LIMIT, path=_STATE_PATH, reset_time=None):
    # Configure the daily limit, state-file path, and reset time.  When
    # reset_time is None it is read from config.json (key CallLimitResetTime).
    self.limit = limit
    self.path = _pathlib.Path(path)
    if reset_time is None:
      reset_time = _read_reset_time()
    self.reset_offset = _parse_reset_time(reset_time)

  def _window_key(self) -> str:
    # ISO date identifying the current 24-hour window.  Shifting "now" back by
    # the reset offset makes the date roll over exactly at the reset time, so
    # each key spans one window regardless of where the reset falls.
    now = _datetime.datetime.now(_UTC)
    return (now - self.reset_offset).date().isoformat()

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
    # Loaded state, rolled over to a fresh count if the window has changed.
    state = self._load()
    window = self._window_key()
    if state.get("date") != window:
      state = {"date": window, "count": 0}
    return state

  def record(self, n: int = 1) -> int:
    # Add n to this window's count, persist it, and return the new count.
    # Refuses (raising CallLimitReached) if the call would take usage past the
    # daily limit, leaving the stored count untouched.
    state = self._current()
    if state["count"] + n > self.limit:
      raise CallLimitReached(
          "BrickLink daily call limit reached: "
          "{}/{} calls used this window".format(state["count"], self.limit)
      )
    state["count"] += n
    self._save(state)
    return state["count"]

  def count(self) -> int:
    # Number of calls recorded so far in the current window.
    return self._current()["count"]

  def remaining(self) -> int:
    # Calls left before the daily limit.  The record() lock keeps this from
    # going negative, though externally-tampered state could still read so.
    return self.limit - self.count()

  def reset(self) -> None:
    # Force this window's count back to 0.
    self._save({"date": self._window_key(), "count": 0})


# Shared default instance and module-level convenience wrappers.
tracker = CallTracker()


def record(n: int = 1) -> int:
  # Record a call against the shared default tracker.
  return tracker.record(n)


def count() -> int:
  # Calls recorded so far this window on the shared default tracker.
  return tracker.count()


def remaining() -> int:
  # Calls left this window on the shared default tracker.
  return tracker.remaining()