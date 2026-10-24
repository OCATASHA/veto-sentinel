"""UTC time helpers and an injectable clock (real or simulated)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; timezone required")
    return dt.astimezone(UTC)


def iso_ms(dt: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and a 'Z' suffix (wire format)."""
    dt = ensure_utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO-8601 timestamp that MUST carry a timezone."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return ensure_utc(dt)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()


class SimClock:
    """Manually advanced clock used for replay and tests."""

    def __init__(self, start: datetime):
        self._now = ensure_utc(start)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, t: datetime) -> None:
        with self._lock:
            self._now = ensure_utc(t)

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            self._now = self._now + delta
            return self._now


def monotonic() -> float:
    return time.monotonic()
