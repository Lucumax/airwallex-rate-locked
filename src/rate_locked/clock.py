"""Clocks. Everything that cares about time takes a clock so tests never sleep."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


class RunningClock:
    """Starts at a fixed scenario time and runs in real time from there.

    The console uses it so quote validity windows tick like real ones while due dates stay relative to the
    fixture's start. `advance` jumps forward; the console offers it in the FAKE world only and logs each use."""

    def __init__(self, start: datetime, monotonic=None):
        import threading
        import time
        self._start = start
        self._mono = monotonic or time.monotonic
        self._t0 = self._mono()
        self._offset = 0.0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._start + timedelta(seconds=self._mono() - self._t0 + self._offset)

    def advance(self, seconds: float) -> datetime:
        with self._lock:
            self._offset += seconds
        return self.now()
