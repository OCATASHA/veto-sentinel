"""Composite Regime Risk Score and degraded-data policy.

    R(t) = w_p * z(P) + w_d * z(D) + w_v * z(V)

Sign convention: every component is oriented so that HIGHER = MORE RISK.
  * P (panic):  source-weighted mean probability that a headline is negative.
  * D (drift):  cosine distance between recent and reference headline centroids.
  * V (volume): log1p(unique headline count) vs. a time-of-day baseline.

Degraded-data policy (never silently substitute zero):
  * A component that is unavailable contributes nothing, and the snapshot is flagged
    ``degraded`` with the reason. The state machine then applies a floor (default CAUTION),
    so the monitor can still escalate but cannot remain in / return to NORMAL.
  * If a component listed in ``required_for_ready`` is unavailable, or NO component is
    available, the monitor is ``not ready`` and the state machine goes to HALT.
  * Weights are not renormalised over the remaining components, so a degraded R(t) is a
    lower bound on what the full score could have been (and is labelled as such).
"""

from __future__ import annotations

import math

from vetosentinel.config import CompositeConfig
from vetosentinel.models import ComponentReading


def composite(readings: dict[str, ComponentReading], cfg: CompositeConfig
              ) -> tuple[float | None, list[str], list[str]]:
    """Return (risk_score, degraded_reasons, not_ready_reasons)."""
    total = 0.0
    used = 0
    degraded: list[str] = []
    not_ready: list[str] = []
    for name, w in cfg.weights.items():
        r = readings.get(name)
        if r is None or not r.available or r.z is None or not math.isfinite(r.z):
            why = (r.reason if r is not None and r.reason else "missing")
            if name in cfg.required_for_ready:
                not_ready.append(f"required component {name} unavailable ({why})")
            elif w > 0:
                degraded.append(f"{name} unavailable ({why})")
            continue
        total += w * r.z
        used += 1
    if used == 0:
        not_ready.append("no risk component available")
        return None, degraded, not_ready
    if not math.isfinite(total):  # defensive: inputs are validated finite already
        not_ready.append("composite score non-finite")
        return None, degraded, not_ready
    return total, degraded, not_ready
