"""Typed configuration.

All defaults below are UNVALIDATED STARTING PARAMETERS chosen to be conservative for a
demonstration. They are not statistically optimised and have not been tuned on any
held-out event period. Override them via a TOML file (see ``config/default.toml``).
"""

from __future__ import annotations

import math
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PARAMETER_STATUS = "unvalidated starting parameters"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PanicConfig(_Strict):
    backend: Literal["auto", "finbert", "lexicon"] = "auto"
    model_name: str = "ProsusAI/finbert"
    window_minutes: float = Field(default=60.0, gt=0)
    min_headlines: int = Field(default=3, ge=1)
    batch_size: int = Field(default=32, ge=1)


class DriftConfig(_Strict):
    backend: Literal["auto", "minilm", "hashing"] = "auto"
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    recent_window_minutes: float = Field(default=60.0, gt=0)
    reference_window_days: float = Field(default=7.0, gt=0)
    min_recent: int = Field(default=5, ge=1)
    min_reference: int = Field(default=50, ge=1)
    bias_correction: bool = True
    hashing_dim: int = Field(default=512, ge=16)


class IntensityConfig(_Strict):
    interval_minutes: float = Field(default=60.0, gt=0)
    lookback_days: int = Field(default=28, ge=1)
    min_history_days: int = Field(default=3, ge=1)
    # Baseline days are matched by day type (weekday vs weekend) and by the same local
    # wall-clock time in this timezone. "UTC" disables DST handling entirely.
    baseline_timezone: str = "UTC"
    split_weekends: bool = True
    # Scale floor from Poisson counting noise: sd(log1p(N)) ~= 1/sqrt(1 + lambda), lambda = baseline median.
    poisson_floor: bool = True


class FocusConfig(_Strict):
    """"Optimize for": weight headlines by relevance to what you trade (see features/relevance.py)."""
    # Comma-separated focus phrases, e.g. "OPEC, crude oil supply, oil tankers". "" = whole market.
    text: str = Field(default="", max_length=300)
    # How much off-focus news still counts (0..1), so a market-wide shock is still noticed.
    background_weight: float = Field(default=0.05, ge=0, le=1)
    # Semantic relevance: robust z of the similarity at which a headline counts as 50% relevant,
    # and the logistic softness in the same units.
    z_mid: float = Field(default=3.0, gt=0)
    z_soft: float = Field(default=0.5, gt=0)
    min_scale: float = Field(default=0.02, gt=0)  # floor for the similarity spread
    calibration_min: int = Field(default=300, ge=20)  # headlines needed before the focus is calibrated
    calibration_max: int = Field(default=5000, ge=100)  # most recent headlines used to calibrate


class NormalizerConfig(_Strict):
    method: Literal["robust", "standard"] = "robust"
    baseline_days: float = Field(default=7.0, gt=0)
    # Exclude the most recent period from the baseline so an emerging shock does not
    # immediately absorb itself into "normal".
    baseline_gap_minutes: float = Field(default=60.0, ge=0)
    min_baseline_samples: int = Field(default=48, ge=3)
    z_clip: float = Field(default=10.0, gt=0)
    # Calm readings in one component cannot offset stress in another when z_floor = 0.
    z_floor: float = Field(default=0.0, le=0)
    min_scale: dict[str, float] = Field(
        default_factory=lambda: {"panic": 0.02, "drift": 0.01, "volume": 0.15}
    )


class CompositeConfig(_Strict):
    weights: dict[str, float] = Field(
        default_factory=lambda: {"panic": 0.4, "drift": 0.3, "volume": 0.3}
    )
    # Components that must be available for the monitor to be "ready" at all.
    required_for_ready: list[str] = Field(default_factory=lambda: ["volume"])

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, w: dict[str, float]) -> dict[str, float]:
        expected = {"panic", "drift", "volume"}
        if set(w) != expected:
            raise ValueError(f"weights must have exactly the keys {sorted(expected)}, got {sorted(w)}")
        for k, v in w.items():
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"weight {k} must be finite and >= 0")
        total = sum(w.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"weights must sum to 1.0 (got {total})")
        return w

    @field_validator("required_for_ready")
    @classmethod
    def _check_required(cls, r: list[str]) -> list[str]:
        bad = set(r) - {"panic", "drift", "volume"}
        if bad:
            raise ValueError(f"unknown components in required_for_ready: {sorted(bad)}")
        return r


class CusumConfig(_Strict):
    # One-sided upper CUSUM on R(t): S = max(0, S + R - k); alarm when S > h.
    k: float = Field(default=1.0, ge=0)
    h: float = Field(default=6.0, gt=0)
    alarm_hold_minutes: float = Field(default=60.0, ge=0)


class StateMachineConfig(_Strict):
    caution_enter: float = 2.0
    caution_exit: float = 1.0
    halt_enter: float = 3.5
    halt_exit: float = 2.5
    cooldown_minutes: float = Field(default=30.0, ge=0)
    # Floor state while some (non-required) components are unavailable.
    degraded_floor: Literal["CAUTION", "HALT"] = "CAUTION"
    # State while the monitor is not ready (warm-up, stale ingestion, required data missing).
    not_ready_state: Literal["HALT"] = "HALT"
    changepoint_escalates: bool = True

    @model_validator(mode="after")
    def _check_hysteresis(self) -> StateMachineConfig:
        if not (self.caution_exit < self.caution_enter):
            raise ValueError("caution_exit must be < caution_enter (hysteresis)")
        if not (self.halt_exit < self.halt_enter):
            raise ValueError("halt_exit must be < halt_enter (hysteresis)")
        if not (self.caution_enter < self.halt_enter):
            raise ValueError("caution_enter must be < halt_enter")
        if not (self.caution_exit <= self.halt_exit):
            raise ValueError("caution_exit must be <= halt_exit")
        return self


class BusConfig(_Strict):
    pub_endpoint: str = "tcp://*:5556"
    topic: str = "vs.v1"
    heartbeat_interval_ms: int = Field(default=1000, ge=50)
    heartbeat_timeout_ms: int = Field(default=3000, ge=100)
    # If the analytics loop has not reported liveness for this long, heartbeats say
    # monitor_ready=false and state=HALT.
    # (Must exceed the slowest single analytics step, e.g. an HTTP fetch timeout.)
    pipeline_stale_ms: int = Field(default=30_000, ge=500)
    # Demo-only fault injection endpoints (silence heartbeats, malformed message, seq gap).
    allow_fault_injection: bool = True
    top_headlines: int = Field(default=5, ge=0, le=20)
    # Reverse channel for the fleet view: bots report their own state here (display only).
    # "auto" = the publish port + 100 on the same host; "" turns it off. Advertised on the bus.
    telemetry_endpoint: str = "auto"

    def telemetry_bind(self) -> str | None:
        if not self.telemetry_endpoint:
            return None
        if self.telemetry_endpoint != "auto":
            return self.telemetry_endpoint
        m = re.fullmatch(r"(tcp://[^:/]+):(\d+)", self.pub_endpoint)
        return f"{m.group(1)}:{int(m.group(2)) + 100}" if m else None

    @model_validator(mode="after")
    def _check(self) -> BusConfig:
        if self.heartbeat_timeout_ms <= self.heartbeat_interval_ms:
            raise ValueError("heartbeat_timeout_ms must exceed heartbeat_interval_ms")
        return self


class AdapterConfig(_Strict):
    kind: Literal["fixture", "gdelt", "gdelt_gkg", "rss", "newsapi"]
    name: str
    enabled: bool = True
    poll_seconds: float = Field(default=300.0, gt=0)
    # fixture
    path: str | None = None
    # gdelt
    query: str | None = None
    max_records: int = Field(default=100, ge=1, le=250)
    # gdelt_gkg
    backfill_days: float = Field(default=0.0, ge=0, le=60)
    themes: list[str] | None = None
    cache_dir: str = "data/gdelt_cache"
    # rss
    url: str | None = None
    source: str | None = None
    # newsapi
    api_key_env: str = "VETOSENTINEL_NEWSAPI_KEY"
    endpoint: str = "https://newsapi.org/v2/top-headlines"
    params: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=15.0, gt=0)


class IngestConfig(_Strict):
    adapters: list[AdapterConfig] = Field(default_factory=list)
    source_weights: dict[str, float] = Field(default_factory=dict)
    default_source_weight: float = Field(default=1.0, gt=0)
    dedup_ttl_days: float = Field(default=7.0, gt=0)
    future_skew_seconds: float = Field(default=300.0, ge=0)
    # FinBERT/MiniLM are English models; other languages are skipped and counted.
    languages: list[str] = Field(default_factory=lambda: ["en", "und"])
    # Live mode: monitor is not ready if no adapter has succeeded within this window.
    stale_after_seconds: float = Field(default=900.0, gt=0)
    # Live: sources such as GDELT publish with a lag, so news is assessed as of the time it is
    # complete. If that falls further behind than this, the monitor is not ready.
    max_data_lag_minutes: float = Field(default=120.0, gt=0)


class ServiceConfig(_Strict):
    tick_seconds: float = Field(default=60.0, gt=0)
    db_path: str = "data/vetosentinel.duckdb"
    # Cache of FinBERT/MiniLM outputs shared across runs (None disables it).
    model_cache_path: str | None = "data/model_cache.sqlite"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000


class ReplayConfig(_Strict):
    tick_minutes: float = Field(default=5.0, gt=0)
    speed: float = Field(default=3600.0, gt=0)  # simulated seconds per wall second
    fast_forward_days: float = Field(default=0.0, ge=0)


class AIConfig(_Strict):
    """Optional AI assistant (Google Gemini): plain-English summaries and focus suggestions.

    Active only when the API key environment variable is set. Advisory only: nothing it writes
    reaches the risk score, the state machine or the signal bus."""
    enabled: bool = True
    api_key_env: str = "GEMINI_API_KEY"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    # Tried in order: the next one is used when a model is overloaded, retired or times out.
    models: list[str] = Field(default_factory=lambda: ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
                              min_length=1)
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_calls_per_minute: int = Field(default=20, ge=1)  # protects the free-tier quota


class WebhookConfig(_Strict):
    kind: Literal["json", "slack", "discord", "ntfy"] = "json"
    url: str = ""  # or, better, url_env: the name of an environment variable holding the URL
    url_env: str = ""
    secret_env: str = ""  # json only: HMAC-SHA256 signing secret (environment variable name)
    name: str = ""


class AlertsConfig(_Strict):
    """Outbound webhook alerts on level changes and operator actions. A target without a URL is
    skipped, so nothing is sent unless someone sets one."""
    enabled: bool = True
    min_interval_seconds: float = Field(default=10.0, ge=0)  # per target; changes in between are folded in
    timeout_seconds: float = Field(default=5.0, gt=0)
    dashboard_url: str = ""  # optional link added to alerts
    webhooks: list[WebhookConfig] = Field(default_factory=list)


class Settings(_Strict):
    panic: PanicConfig = Field(default_factory=PanicConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    intensity: IntensityConfig = Field(default_factory=IntensityConfig)
    normalizer: NormalizerConfig = Field(default_factory=NormalizerConfig)
    composite: CompositeConfig = Field(default_factory=CompositeConfig)
    cusum: CusumConfig = Field(default_factory=CusumConfig)
    state_machine: StateMachineConfig = Field(default_factory=StateMachineConfig)
    focus: FocusConfig = Field(default_factory=FocusConfig)
    bus: BusConfig = Field(default_factory=BusConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    parameter_status: str = PARAMETER_STATUS


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        return Settings()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
