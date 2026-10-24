"""Change-point detector interface.

A detector consumes one finite observation per tick and reports whether a change is
currently evidenced. Implementations must be deterministic given their inputs so replays
are reproducible. Missing observations (``None``) must not be treated as zeros: detectors
skip the update and report their current statistic unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from vetosentinel.models import ChangePointResult


class ChangeDetector(Protocol):
    name: str

    def update(self, t: datetime, x: float | None) -> ChangePointResult: ...

    def reset(self) -> None: ...
