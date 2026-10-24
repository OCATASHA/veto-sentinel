"""Typed data contracts shared across the pipeline."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    HALT = "HALT"
    FLATTEN = "FLATTEN"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    @staticmethod
    def max(a: RiskState, b: RiskState) -> RiskState:
        return a if a.severity >= b.severity else b


_SEVERITY = {RiskState.NORMAL: 0, RiskState.CAUTION: 1, RiskState.HALT: 2, RiskState.FLATTEN: 3}


class DataOrigin(StrEnum):
    LIVE = "live_provider"
    HISTORICAL = "historical_dataset"
    SYNTHETIC = "synthetic_fixture"


class TimestampQuality(StrEnum):
    PROVIDER = "provider"  # publisher-supplied timestamp with timezone
    PROVIDER_SEEN = "provider_seen"  # aggregator first-seen time (e.g. GDELT seendate)
    ASSUMED_UTC = "assumed_utc"  # timestamp had no timezone; UTC assumed
    MISSING = "missing"  # no published time; fetched_at used as event time
    FUTURE_CLAMPED = "future_clamped"  # published time was in the future; clamped to fetched_at
    UNPARSEABLE = "unparseable"  # could not parse; fetched_at used as event time


class Headline(BaseModel):
    model_config = ConfigDict(frozen=True)

    headline_id: str
    headline: str = Field(min_length=1)
    source: str
    url: str | None = None
    canonical_url: str | None = None
    text_fingerprint: str
    published_at: datetime | None
    fetched_at: datetime
    # The time used for windowing: published_at when trustworthy, else fetched_at.
    event_time: datetime
    timestamp_quality: TimestampQuality
    language: str = "und"
    source_weight: float = Field(1.0, gt=0)
    # How many headlines this one stands for in the news-volume count (an hourly archive sample
    # stands for four 15-minute files). Does not affect negativity or topic averages.
    count_weight: float = Field(1.0, gt=0)
    provider: str
    data_origin: DataOrigin

    @field_validator("published_at", "fetched_at", "event_time")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware UTC")
        return v


class RawItem(BaseModel):
    """What an adapter yields before normalisation. Everything is optional/untrusted."""

    headline: str | None = None
    source: str | None = None
    url: str | None = None
    published_raw: str | None = None
    published_quality_hint: TimestampQuality | None = None
    language: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ComponentReading(BaseModel):
    """One feature at one tick, with everything needed to audit it later."""

    name: str  # panic | drift | volume
    available: bool
    raw: float | None = None
    z: float | None = None  # normalised value, clipped/floored; None if unavailable
    z_unclipped: float | None = None
    sample_count: int = 0
    baseline_count: int = 0
    baseline_center: float | None = None
    baseline_scale: float | None = None
    baseline_ready: bool = False
    reason: str = ""  # why unavailable / diagnostics
    implementation: str = ""
    is_fallback: bool = False

    @field_validator("raw", "z", "z_unclipped", "baseline_center", "baseline_scale")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("non-finite value in component reading")
        return v


class ChangePointResult(BaseModel):
    detector: str
    statistic: float
    threshold: float
    alarm: bool  # alarm fired on this update
    alarm_active: bool  # within the hold period after an alarm
    detail: str = ""


class RiskSnapshot(BaseModel):
    as_of: datetime
    components: dict[str, ComponentReading]
    risk_score: float | None  # None when no component is usable
    monitor_ready: bool
    not_ready_reasons: list[str] = Field(default_factory=list)
    degraded: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    top_headline_ids: list[str] = Field(default_factory=list)
    data_origins: list[str] = Field(default_factory=list)

    @field_validator("risk_score")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("risk_score must be finite")
        return v


class DecisionRecord(BaseModel):
    as_of: datetime  # data time of the assessment (simulated during replay)
    wall_ts: datetime  # when the decision was computed
    prev_state: RiskState
    new_state: RiskState
    changed: bool
    rule: str
    reason: str
    risk_score: float | None
    components: dict[str, float | None]
    changepoint: ChangePointResult | None
    headline_ids: list[str] = Field(default_factory=list)
    health: dict[str, Any] = Field(default_factory=dict)
