"""Rolling z-score normalisation with explicit protections.

Contract: ``RollingNormalizer.score(t, raw)`` returns ``(z, z_unclipped, info)`` where ``z``
is finite or ``None``. It never returns NaN/inf. The baseline is the feature's own history in
``[t - baseline_days, t - baseline_gap]`` so that an emerging shock is not immediately absorbed
into "normal". Raw values are only added to history via ``observe``.

Protections:
* robust method uses median and MAD * 1.4826 (resistant to outliers in the baseline);
* the scale is floored at ``min_scale`` so zero/near-zero variance cannot explode z;
* z is clipped to ``[-z_clip, z_clip]`` and then floored at ``z_floor`` (default 0: calm
  readings cannot offset stressed ones in the composite);
* fewer than ``min_baseline_samples`` -> baseline not ready -> z is None.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from vetosentinel.config import NormalizerConfig


@dataclass
class NormInfo:
    baseline_count: int
    center: float | None
    scale: float | None
    ready: bool
    reason: str = ""


def robust_z(value: float, baseline: np.ndarray, method: str, min_scale: float,
             z_clip: float, z_floor: float) -> tuple[float, float, float, float]:
    """Return (z, z_unclipped, center, scale) for a finite value against a finite baseline."""
    if method == "robust":
        center = float(np.median(baseline))
        scale = float(np.median(np.abs(baseline - center)) * 1.4826)
    else:
        center = float(np.mean(baseline))
        scale = float(np.std(baseline, ddof=1)) if baseline.size > 1 else 0.0
    if not math.isfinite(scale) or scale < min_scale:
        scale = min_scale
    z_raw = (value - center) / scale
    if not math.isfinite(z_raw):
        z_raw = math.copysign(z_clip, z_raw) if not math.isnan(z_raw) else 0.0
    z = max(min(z_raw, z_clip), -z_clip)
    z = max(z, z_floor)
    return z, z_raw, center, scale


class RollingNormalizer:
    def __init__(self, name: str, cfg: NormalizerConfig):
        self.name = name
        self.cfg = cfg
        self.min_scale = float(cfg.min_scale.get(name, 1e-3))
        self._t: list[datetime] = []
        self._v: list[float] = []

    def observe(self, t: datetime, raw: float) -> None:
        if raw is None or not math.isfinite(raw):
            return
        i = bisect.bisect_right(self._t, t)
        self._t.insert(i, t)
        self._v.insert(i, float(raw))
        self._prune(t)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.cfg.baseline_days) - timedelta(hours=1)
        i = bisect.bisect_left(self._t, cutoff)
        if i > 0:
            del self._t[:i]
            del self._v[:i]

    def baseline(self, t: datetime) -> np.ndarray:
        lo = t - timedelta(days=self.cfg.baseline_days)
        hi = t - timedelta(minutes=self.cfg.baseline_gap_minutes)
        i = bisect.bisect_left(self._t, lo)
        j = bisect.bisect_right(self._t, hi)
        return np.asarray(self._v[i:j], dtype=float)

    def score(self, t: datetime, raw: float | None) -> tuple[float | None, float | None, NormInfo]:
        base = self.baseline(t)
        n = int(base.size)
        if raw is None or not math.isfinite(raw):
            return None, None, NormInfo(n, None, None, n >= self.cfg.min_baseline_samples,
                                        "raw value unavailable")
        if n < self.cfg.min_baseline_samples:
            return None, None, NormInfo(n, None, None, False,
                                        f"baseline warm-up: {n}/{self.cfg.min_baseline_samples} samples")
        z, z_raw, c, s = robust_z(raw, base, self.cfg.method, self.min_scale,
                                  self.cfg.z_clip, self.cfg.z_floor)
        return z, z_raw, NormInfo(n, c, s, True)
