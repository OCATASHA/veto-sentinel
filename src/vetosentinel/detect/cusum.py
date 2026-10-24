"""One-sided (upper) CUSUM change detector on the composite risk score.

    S_t = max(0, S_{t-1} + x_t - k)      alarm when S_t > h

``x_t`` is the composite score R(t), which is already expressed relative to rolling
baselines, so under "normal" conditions it hovers near a small non-negative mean. ``k`` is
the allowance (reference value) and ``h`` the decision threshold, both in units of R.
After an alarm the statistic resets to 0 and the alarm stays *active* for
``alarm_hold_minutes`` of data time so the state machine can act on it.

Assumptions (documented, not validated): observations are roughly independent between
ticks, and the in-control mean of R is below ``k``. Neither is true exactly for overlapping
rolling windows, so treat ``h`` as a tunable sensitivity knob, not a calibrated false-alarm rate.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from vetosentinel.config import CusumConfig
from vetosentinel.models import ChangePointResult


class CusumDetector:
    name = "cusum"

    def __init__(self, cfg: CusumConfig):
        self.cfg = cfg
        self.s = 0.0
        self.last_alarm_at: datetime | None = None
        self.alarm_count = 0

    def reset(self) -> None:
        self.s = 0.0
        self.last_alarm_at = None

    def _active(self, t: datetime) -> bool:
        if self.last_alarm_at is None:
            return False
        return t - self.last_alarm_at <= timedelta(minutes=self.cfg.alarm_hold_minutes)

    def hold(self, t: datetime) -> ChangePointResult:
        """No new news time since the last update: report the statistic unchanged."""
        return ChangePointResult(detector=self.name, statistic=self.s, threshold=self.cfg.h, alarm=False,
                                 alarm_active=self._active(t), detail="no new news time; statistic unchanged")

    def update(self, t: datetime, x: float | None) -> ChangePointResult:
        if x is None or not math.isfinite(x):
            return ChangePointResult(detector=self.name, statistic=self.s, threshold=self.cfg.h,
                                     alarm=False, alarm_active=self._active(t),
                                     detail="no valid observation; statistic not updated")
        self.s = max(0.0, self.s + x - self.cfg.k)
        alarm = self.s > self.cfg.h
        detail = f"S={self.s:.3f} after x={x:.3f}, k={self.cfg.k}"
        stat = self.s
        if alarm:
            self.last_alarm_at = t
            self.alarm_count += 1
            detail = f"ALARM: S={self.s:.3f} > h={self.cfg.h}; statistic reset"
            self.s = 0.0
        return ChangePointResult(detector=self.name, statistic=stat, threshold=self.cfg.h,
                                 alarm=alarm, alarm_active=self._active(t), detail=detail)
