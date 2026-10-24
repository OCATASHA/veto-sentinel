"""Panic score P(t): source-weighted mean negative-sentiment probability.

Input:  per-headline (event_time, p_negative in [0,1], source_weight > 0, headline_id).
Output: P(t) = sum(w_i * p_i) / sum(w_i) over headlines with event_time in (t - window, t].
        Unavailable when fewer than ``min_headlines`` unique headlines fall in the window.
Higher P => more negative coverage => more risk.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from vetosentinel.config import PanicConfig


@dataclass(frozen=True)
class PanicResult:
    value: float | None
    count: int
    reason: str
    top: list[tuple[str, float]]  # (headline_id, weighted negativity), most negative first


class PanicAggregator:
    def __init__(self, cfg: PanicConfig, retention: timedelta = timedelta(days=2)):
        self.cfg = cfg
        self.retention = retention
        self._t: list[datetime] = []
        self._rows: list[tuple[float, float, str]] = []  # (p_neg, weight, headline_id)

    def add(self, t: datetime, p_neg: float, weight: float, headline_id: str) -> bool:
        if not (math.isfinite(p_neg) and 0.0 <= p_neg <= 1.0 and math.isfinite(weight) and weight > 0):
            return False
        i = bisect.bisect_right(self._t, t)
        self._t.insert(i, t)
        self._rows.insert(i, (p_neg, weight, headline_id))
        return True

    def prune(self, now: datetime) -> None:
        i = bisect.bisect_left(self._t, now - self.retention)
        if i:
            del self._t[:i]
            del self._rows[:i]

    def compute(self, t: datetime, top_n: int = 5) -> PanicResult:
        lo = t - timedelta(minutes=self.cfg.window_minutes)
        i = bisect.bisect_right(self._t, lo)
        j = bisect.bisect_right(self._t, t)
        rows = self._rows[i:j]
        n = len(rows)
        if n < self.cfg.min_headlines:
            return PanicResult(None, n, f"only {n} headlines in {self.cfg.window_minutes:g}m window "
                                        f"(min {self.cfg.min_headlines})", [])
        wsum = sum(w for _, w, _ in rows)
        value = sum(p * w for p, w, _ in rows) / wsum
        top = sorted(((hid, p * w) for p, w, hid in rows), key=lambda x: -x[1])[:top_n]
        return PanicResult(value, n, "", top)
