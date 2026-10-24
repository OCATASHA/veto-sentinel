"""Deduplication by canonical URL and normalised-text fingerprint.

A headline is a duplicate if EITHER its canonical URL or its text fingerprint was seen
within the TTL (keyed on event time). Syndicated copies of the same story from different
outlets therefore count once; this is deliberate (they are not independent events).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from vetosentinel.models import Headline


@dataclass(frozen=True)
class DupDecision:
    duplicate: bool
    reason: str = ""
    original_id: str | None = None


class Deduplicator:
    def __init__(self, ttl: timedelta = timedelta(days=7)):
        self.ttl = ttl
        self._urls: OrderedDict[str, tuple[datetime, str]] = OrderedDict()
        self._texts: OrderedDict[str, tuple[datetime, str]] = OrderedDict()

    def check_and_add(self, h: Headline) -> DupDecision:
        self._expire(h.event_time)
        if h.canonical_url and h.canonical_url in self._urls:
            return DupDecision(True, "canonical_url", self._urls[h.canonical_url][1])
        if h.text_fingerprint in self._texts:
            return DupDecision(True, "text_fingerprint", self._texts[h.text_fingerprint][1])
        if h.canonical_url:
            self._urls[h.canonical_url] = (h.event_time, h.headline_id)
        self._texts[h.text_fingerprint] = (h.event_time, h.headline_id)
        return DupDecision(False)

    def _expire(self, now: datetime) -> None:
        cutoff = now - self.ttl
        for d in (self._urls, self._texts):
            while d:
                k, (t, _) = next(iter(d.items()))
                if t >= cutoff:
                    break
                d.popitem(last=False)
