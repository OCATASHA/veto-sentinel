"""Panic score P(t): source-weighted mean negative-sentiment probability.

Input:  per-headline (event_time, p_negative in [0,1], source_weight a > 0, headline_id,
        focus relevance weight r in [0, 1], 1 without a focus - see relevance.py).
Output: P(t) = sum(a_i r_i p_i) / sum(a_i r_i) over headlines with event_time in (t - window, t].
        Unavailable when the effective number of headlines n_eff = (sum r)^2 / sum r^2 (= the
        plain count without a focus) is below ``min_headlines``.
Higher P => more negative coverage => more risk.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
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
        self._rows: list[tuple[float, float, str, float]] = []  # (p_neg, source weight, id, relevance w)

    def add(self, t: datetime, p_neg: float, weight: float, headline_id: str, rel: float = 1.0) -> bool:
        if not (math.isfinite(p_neg) and 0.0 <= p_neg <= 1.0 and math.isfinite(weight) and weight > 0):
            return False
        i = bisect.bisect_right(self._t, t)
        self._t.insert(i, t)
        self._rows.insert(i, (p_neg, weight, headline_id, rel))
        return True

    def reweight(self, rel: Callable[[str], float]) -> None:
        """Replace every headline's relevance weight (when the focus changes)."""
        self._rows = [(p, a, hid, rel(hid)) for p, a, hid, _ in self._rows]

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
        r1 = sum(r for *_, r in rows)
        r2 = sum(r * r for *_, r in rows)
        weighted = any(r != 1.0 for *_, r in rows)
        n_eff = r1 * r1 / r2 if r2 > 0 else 0.0
        count = int(round(n_eff)) if weighted else n
        if n_eff < self.cfg.min_headlines:
            what = f"{n_eff:.1f} effective headlines (relevance-weighted)" if weighted else f"{n} headlines"
            return PanicResult(None, count, f"only {what} in {self.cfg.window_minutes:g}m window "
                                            f"(min {self.cfg.min_headlines})", [])
        wsum = sum(a * r for _, a, _, r in rows)
        value = sum(p * a * r for p, a, _, r in rows) / wsum
        top = sorted(((hid, p * a * r) for p, a, hid, r in rows), key=lambda x: -x[1])[:top_n]
        return PanicResult(value, count, "", top)
