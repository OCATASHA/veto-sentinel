"""Feature pipeline: headlines -> (P, D, V) readings -> z-scores -> composite R(t).

Contract:
  ``ingest(headlines)`` scores each new unique headline (sentiment + embedding) and adds it to
  the panic, drift and intensity trackers. Model failures do not raise; the affected headlines
  simply do not contribute to that component and the failure is reported in diagnostics.
  ``compute(t)`` returns a ``RiskSnapshot`` with raw values, z-scores, sample counts, baseline
  status and implementation labels for every component. It never emits NaN/inf.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from vetosentinel.config import Settings
from vetosentinel.features.backends import EmbeddingBackend, SentimentBackend
from vetosentinel.features.composite import composite
from vetosentinel.features.drift import DriftTracker
from vetosentinel.features.intensity import IntensityTracker
from vetosentinel.features.normalize import RollingNormalizer
from vetosentinel.features.panic import PanicAggregator
from vetosentinel.models import ComponentReading, Headline, RiskSnapshot

log = logging.getLogger(__name__)


class FeaturePipeline:
    def __init__(self, settings: Settings, sentiment: SentimentBackend, embedder: EmbeddingBackend):
        self.s = settings
        self.sentiment = sentiment
        self.embedder = embedder
        self.panic = PanicAggregator(settings.panic,
                                     retention=timedelta(minutes=settings.panic.window_minutes) + timedelta(hours=1))
        self.drift = DriftTracker(settings.drift, embedder.dim)
        self.intensity = IntensityTracker(settings.intensity, settings.normalizer)
        self.norm = {"panic": RollingNormalizer("panic", settings.normalizer),
                     "drift": RollingNormalizer("drift", settings.normalizer)}
        self.sentiment_errors = 0
        self.embed_errors = 0
        self.last_model_error = ""
        self.invalid_sentiment = 0
        self.origins: set[str] = set()

    @property
    def implementations(self) -> dict[str, dict]:
        return {
            "panic": {"implementation": self.sentiment.implementation, "is_fallback": self.sentiment.is_fallback},
            "drift": {"implementation": self.embedder.implementation, "is_fallback": self.embedder.is_fallback},
            "volume": {"implementation": "unique-count-vs-time-of-day-v1", "is_fallback": False},
        }

    def ingest(self, headlines: list[Headline]) -> dict[str, float]:
        """Score and add headlines. Returns {headline_id: p_negative} for those scored."""
        if not headlines:
            return {}
        texts = [h.headline for h in headlines]
        probs: list[float] | None = None
        try:
            probs = self.sentiment.negative_probs(texts)
            if len(probs) != len(texts):
                raise RuntimeError(f"sentiment returned {len(probs)} scores for {len(texts)} texts")
        except Exception as e:  # noqa: BLE001
            self.sentiment_errors += 1
            self.last_model_error = f"sentiment: {type(e).__name__}: {e}"
            log.error("sentiment scoring failed: %s", self.last_model_error)
            probs = None
        embs = None
        try:
            embs = self.embedder.embed(texts)
            if embs.shape[0] != len(texts):
                raise RuntimeError("embedding count mismatch")
        except Exception as e:  # noqa: BLE001
            self.embed_errors += 1
            self.last_model_error = f"embedding: {type(e).__name__}: {e}"
            log.error("embedding failed: %s", self.last_model_error)
            embs = None

        scored: dict[str, float] = {}
        for i, h in enumerate(headlines):
            self.origins.add(str(h.data_origin))
            self.intensity.add(h.event_time, h.count_weight)
            if probs is not None:
                p = float(probs[i])
                if self.panic.add(h.event_time, p, h.source_weight, h.headline_id):
                    scored[h.headline_id] = p
                else:
                    self.invalid_sentiment += 1
        if embs is not None:
            self.drift.add_many([(h.event_time, embs[i]) for i, h in enumerate(headlines)])
        return scored

    def compute(self, t: datetime) -> RiskSnapshot:
        impl = self.implementations
        readings: dict[str, ComponentReading] = {}

        pr = self.panic.compute(t)
        readings["panic"] = self._normalised("panic", t, pr.value, pr.count, pr.reason, impl["panic"])

        dr = self.drift.compute(t)
        readings["drift"] = self._normalised("drift", t, dr.value, dr.recent_count, dr.reason, impl["drift"])

        ir = self.intensity.compute(t)
        readings["volume"] = ComponentReading(
            name="volume", available=ir.z is not None, raw=None if ir.count is None else float(ir.count),
            z=ir.z, z_unclipped=ir.z_unclipped, sample_count=int(round(ir.count or 0)), baseline_count=ir.history_days,
            baseline_center=ir.center, baseline_scale=ir.scale, baseline_ready=ir.z is not None,
            reason=ir.reason, **impl["volume"])

        score, degraded, not_ready = composite(readings, self.s.composite)
        return RiskSnapshot(
            as_of=t, components=readings, risk_score=score, monitor_ready=not not_ready,
            not_ready_reasons=not_ready, degraded=bool(degraded), degraded_reasons=degraded,
            top_headline_ids=[hid for hid, _ in pr.top], data_origins=sorted(self.origins))

    def _normalised(self, name: str, t: datetime, raw: float | None, n: int, reason: str,
                    impl: dict) -> ComponentReading:
        norm = self.norm[name]
        z, z_raw, info = norm.score(t, raw)
        if raw is not None and math.isfinite(raw):
            norm.observe(t, raw)
        why = reason or info.reason
        return ComponentReading(
            name=name, available=z is not None, raw=raw, z=z, z_unclipped=z_raw, sample_count=n,
            baseline_count=info.baseline_count, baseline_center=info.center, baseline_scale=info.scale,
            baseline_ready=info.ready, reason=why, **impl)

    def prune(self, t: datetime) -> None:
        self.panic.prune(t)
        self.drift.prune(t)
        self.intensity.prune(t)
