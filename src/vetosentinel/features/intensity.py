"""News intensity V(t): unique-headline count vs. a time-of-day-aware baseline.

    count(t)    = # unique headlines with event_time in (t - interval, t]
    baseline(t) = { count(t_k) : k = 1..lookback_days }, where t_k is the SAME LOCAL WALL-CLOCK
                  time k days earlier in ``baseline_timezone``, restricted to days of the same
                  type (weekday vs weekend) when ``split_weekends``.
    z(V)        = robust z of log1p(count(t)) against log1p(baseline)

Time handling: all stored times are UTC. ``baseline_timezone`` (IANA name, default UTC)
only decides which earlier instants are "the same time of day"; with e.g.
``America/New_York`` the baseline follows US market hours across DST changes. Nonexistent
or ambiguous local times (DST transitions) resolve with ``fold=0`` via zoneinfo.

Scale protection: besides ``min_scale``, the scale is floored at the Poisson counting
noise of the baseline level, 1/sqrt(1 + median count) in log1p units, because a few
history days can show spuriously low dispersion (especially at quiet hours).

Missing history: a baseline day is used only if its whole window lies inside the
observed coverage and does not overlap a recorded ingestion gap. The current window must
also be fully covered. Fewer than ``min_history_days`` usable days -> not ready.
Higher V => more news than usual for this time => more risk.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from vetosentinel.config import IntensityConfig, NormalizerConfig
from vetosentinel.features.normalize import robust_z


@dataclass(frozen=True)
class IntensityResult:
    count: float | None
    z: float | None
    z_unclipped: float | None
    history_days: int
    center: float | None
    scale: float | None
    reason: str


class IntensityTracker:
    def __init__(self, cfg: IntensityConfig, norm: NormalizerConfig):
        self.cfg = cfg
        self.norm = norm
        self.tz = ZoneInfo(cfg.baseline_timezone)
        self._t: list[datetime] = []
        self._w: list[float] = []
        self._weighted = False  # all weights 1 -> plain bisect counting
        self._cum: np.ndarray | None = None
        self.coverage_start: datetime | None = None
        self.gaps: list[tuple[datetime, datetime]] = []

    def set_coverage_start(self, t: datetime) -> None:
        if self.coverage_start is None or t < self.coverage_start:
            self.coverage_start = t

    def mark_gap(self, start: datetime, end: datetime) -> None:
        if end > start:
            self.gaps.append((start, end))

    def add(self, t: datetime, weight: float = 1.0) -> None:
        # Coverage is set explicitly by the monitor (fixture start, first successful live fetch,
        # or a declared backfill): stray old items (e.g. an RSS feed's last N entries) must not
        # make earlier days look like genuinely observed low-volume history.
        i = bisect.bisect_right(self._t, t)
        self._t.insert(i, t)
        self._w.insert(i, weight)
        if weight != 1.0:
            self._weighted = True
        self._cum = None

    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.cfg.lookback_days + 2)
        i = bisect.bisect_left(self._t, cutoff)
        if i:
            del self._t[:i]
            del self._w[:i]
            self._cum = None
        self.gaps = [g for g in self.gaps if g[1] >= cutoff]

    def count(self, lo: datetime, hi: datetime) -> float:
        i, j = bisect.bisect_right(self._t, lo), bisect.bisect_right(self._t, hi)
        if not self._weighted:
            return j - i
        if self._cum is None:
            self._cum = np.concatenate([[0.0], np.cumsum(self._w)])
        return float(self._cum[j] - self._cum[i])

    def _covered(self, lo: datetime, hi: datetime) -> bool:
        if self.coverage_start is None or lo < self.coverage_start:
            return False
        return not any(gs < hi and ge > lo for gs, ge in self.gaps)

    def _is_weekend(self, t: datetime) -> bool:
        return t.astimezone(self.tz).weekday() >= 5

    def same_local_time_days_ago(self, t: datetime, k: int) -> datetime:
        local = t.astimezone(self.tz)
        prev = datetime.combine(local.date() - timedelta(days=k), local.time().replace(tzinfo=None),
                                tzinfo=self.tz)
        return prev.astimezone(UTC)

    def compute(self, t: datetime) -> IntensityResult:
        iv = timedelta(minutes=self.cfg.interval_minutes)
        if not self._covered(t - iv, t):
            return IntensityResult(None, None, None, 0, None, None,
                                   "current window not fully covered by ingestion")
        cur = self.count(t - iv, t)
        weekend = self._is_weekend(t)
        hist: list[float] = []
        for k in range(1, self.cfg.lookback_days + 1):
            tk = self.same_local_time_days_ago(t, k)
            if self.cfg.split_weekends and self._is_weekend(tk) != weekend:
                continue
            if not self._covered(tk - iv, tk):
                continue
            hist.append(self.count(tk - iv, tk))
        n = len(hist)
        if n < self.cfg.min_history_days:
            return IntensityResult(cur, None, None, n, None, None,
                                   f"time-of-day baseline warm-up: {n}/{self.cfg.min_history_days} "
                                   f"matching {'weekend' if weekend else 'weekday'} days")
        base = np.log1p(np.asarray(hist, dtype=float))
        floor = float(self.norm.min_scale.get("volume", 0.15))
        if self.cfg.poisson_floor:
            floor = max(floor, 1.0 / math.sqrt(1.0 + float(np.median(hist))))
        z, z_raw, c, s = robust_z(math.log1p(cur), base, self.norm.method, floor,
                                  self.norm.z_clip, self.norm.z_floor)
        return IntensityResult(cur, z, z_raw, n, c, s, "")
