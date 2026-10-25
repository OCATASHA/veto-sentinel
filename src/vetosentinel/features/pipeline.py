"""Feature pipeline: headlines -> (P, D, V) readings -> z-scores -> composite R(t).

Contract:
  ``ingest(headlines)`` scores each new unique headline (sentiment + embedding) and adds it to
  the panic, drift and intensity trackers. Model failures do not raise; the affected headlines
  simply do not contribute to that component and the failure is reported in diagnostics.
  ``compute(t)`` returns a ``RiskSnapshot`` with raw values, z-scores, sample counts, baseline
  status and implementation labels for every component. It never emits NaN/inf.
  ``set_focus(text)`` ("Optimize for") weights every headline by its relevance to the focus
  (see relevance.py): retained headlines are re-weighted and the z-score baselines rebuilt, so
  they always use the same weights as the current reading.
"""

from __future__ import annotations

import bisect
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from vetosentinel.config import Settings
from vetosentinel.features.backends import EmbeddingBackend, SentimentBackend
from vetosentinel.features.composite import composite
from vetosentinel.features.drift import DriftTracker
from vetosentinel.features.intensity import IntensityTracker
from vetosentinel.features.normalize import RollingNormalizer
from vetosentinel.features.panic import PanicAggregator
from vetosentinel.features.relevance import RelevanceModel, parse_focus
from vetosentinel.features.trending import terms, trending
from vetosentinel.models import ComponentReading, Headline, RiskSnapshot

log = logging.getLogger(__name__)


class FeaturePipeline:
    def __init__(self, settings: Settings, sentiment: SentimentBackend, embedder: EmbeddingBackend):
        self.s = settings
        self.sentiment = sentiment
        self.embedder = embedder
        # Panic rows are kept for the whole baseline period so a focus change can recompute it.
        self.panic = PanicAggregator(settings.panic, retention=timedelta(days=settings.normalizer.baseline_days)
                                     + timedelta(minutes=settings.panic.window_minutes) + timedelta(hours=2))
        self.drift = DriftTracker(settings.drift, embedder.dim)
        self.intensity = IntensityTracker(settings.intensity, settings.normalizer)
        self.norm = {"panic": RollingNormalizer("panic", settings.normalizer),
                     "drift": RollingNormalizer("drift", settings.normalizer)}
        self.sentiment_errors = 0
        self.embed_errors = 0
        self.last_model_error = ""
        self.invalid_sentiment = 0
        self.origins: set[str] = set()
        # Focus ("Optimize for"): retained headlines (time order) so their weights can be redone.
        self.focus: RelevanceModel | None = None
        self._items_t: list[datetime] = []
        # (id, text, vector, count weight, terms for trending words, source)
        self._items: list[tuple[str, str, np.ndarray | None, float, tuple[str, ...], str | None]] = []
        self._rel: dict[str, float] = {}  # relevance r of each retained headline (with a focus)
        self._needs_rebuild = False
        self._effect_cache: tuple[Any, dict[str, Any]] | None = None
        self.weights_version = 0  # bumped whenever headline weights change (focus set or calibrated)
        self.model_timing: dict[str, Any] = {}  # last batch: AI model time per headline (speed budget)
        if settings.focus.text.strip():
            self.set_focus(settings.focus.text, settings.focus.background_weight)

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
        t0 = time.perf_counter()
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

        dt = time.perf_counter() - t0
        self.model_timing = {"headlines": len(texts), "ms_per_headline": dt * 1000 / len(texts),
                             "batch_ms": dt * 1000}
        vecs: list[np.ndarray | None] = [None] * len(headlines) if embs is None else list(embs)
        self._remember(headlines, vecs)
        f = self.focus
        if f is not None and f.calibrated:
            for h, r in zip(headlines, f.relevance(texts, vecs), strict=True):
                self._rel[h.headline_id] = float(r)
        weights = [self.weight(h.headline_id) for h in headlines]
        scored: dict[str, float] = {}
        for i, h in enumerate(headlines):
            self.origins.add(str(h.data_origin))
            self.intensity.add(h.event_time, h.count_weight, weights[i], h.headline_id)
            if probs is not None:
                p = float(probs[i])
                if self.panic.add(h.event_time, p, h.source_weight, h.headline_id, weights[i]):
                    scored[h.headline_id] = p
                else:
                    self.invalid_sentiment += 1
        if embs is not None:
            self.drift.add_many([(h.event_time, embs[i], weights[i], h.headline_id) for i, h in enumerate(headlines)])
        return scored

    # ------------------------------------------------------------------ focus ("Optimize for")
    def weight(self, headline_id: str) -> float:
        """How much a headline counts: 1 without a focus, else b + (1 - b) * relevance."""
        f = self.focus
        if f is None or not f.calibrated:
            return 1.0
        return f.background + (1.0 - f.background) * self._rel.get(headline_id, 0.0)

    def relevance_of(self, headline_id: str) -> float | None:
        return self._rel.get(headline_id) if self.focus is not None and self.focus.calibrated else None

    def _remember(self, headlines: list[Headline], vecs: list[np.ndarray | None]) -> None:
        for h, v in zip(headlines, vecs, strict=True):
            i = bisect.bisect_right(self._items_t, h.event_time)
            self._items_t.insert(i, h.event_time)
            self._items.insert(i, (h.headline_id, h.headline, None if v is None else np.asarray(v, dtype=np.float16),
                                   h.count_weight, terms(h.headline)[0], h.source))
        f = self.focus
        if f is not None and not f.calibrated and f.calibrate(self._vectors()):
            self._apply_focus()  # first calibration: weights change for everything seen so far
            self._needs_rebuild = True

    def _vectors(self) -> np.ndarray:
        """Vectors of retained headlines, oldest first (for calibration)."""
        vs = [x[2] for x in self._items if x[2] is not None]
        return np.stack(vs).astype(np.float32) if vs else np.zeros((0, self.embedder.dim), dtype=np.float32)

    def _model(self, text: str, background: float | None) -> RelevanceModel | None:
        if not parse_focus(text):
            return None
        cfg = self.s.focus.model_copy(update={"background_weight": self.s.focus.background_weight
                                              if background is None else float(background)})
        model = RelevanceModel(text, cfg, self.embedder)
        model.calibrate(self._vectors())
        return model

    def _apply_focus(self) -> None:
        """Recompute every retained headline's relevance and re-weight all trackers."""
        f = self.focus
        self._rel = {}
        if f is not None and f.calibrated and self._items:
            r = f.relevance([x[1] for x in self._items], [x[2] for x in self._items])
            self._rel = {x[0]: float(ri) for x, ri in zip(self._items, r, strict=True)}
        self.panic.reweight(self.weight)
        self.drift.reweight(self.weight)
        self.intensity.reweight(self.weight)
        self._effect_cache = None
        self.weights_version += 1

    def set_focus(self, text: str, background: float | None = None,
                  rebuild_until: datetime | None = None) -> dict[str, Any]:
        """Optimize for ``text`` ("" = whole market). With ``rebuild_until`` the z-score baselines
        are recomputed up to (not including) that time with the new weights."""
        if background is not None and not 0.0 <= background <= 1.0:
            raise ValueError("background weight must be between 0 and 1")
        self.focus = self._model(text, background)
        self.s.focus.text = self.focus.text if self.focus else ""
        if background is not None:
            self.s.focus.background_weight = float(background)
        self._apply_focus()
        if rebuild_until is not None:
            self.rebuild_baselines(rebuild_until)
        return self.focus_info()

    def reset_baselines(self) -> None:
        self.norm = {name: RollingNormalizer(name, self.s.normalizer) for name in self.norm}
        self._needs_rebuild = False

    def rebuild_baselines(self, until: datetime, step: timedelta = timedelta(minutes=10)) -> int:
        """Recompute the panic/drift z-score history over the baseline period before ``until``
        (exclusive) from retained headlines, with the current weights. Returns the step count."""
        self.reset_baselines()
        if not self._items_t:
            return 0
        span = timedelta(days=self.s.normalizer.baseline_days, minutes=self.s.normalizer.baseline_gap_minutes)
        start = max(self._items_t[0], until - span)
        k = int((until - start) / step)
        n = 0
        for i in range(k, 0, -1):  # on a grid that ends one step before ``until``
            self.compute(until - i * step)
            n += 1
        return n

    def preview_focus(self, text: str, background: float | None, now: datetime | None = None,
                      top_n: int = 8) -> dict[str, Any]:
        """What ``set_focus`` would do, without applying it: matches among the last day's news."""
        model = self._model(text, background)
        if model is None:
            return {"text": "", "phrases": []}
        info = model.info()
        if not model.calibrated:
            n = len(self._vectors())
            return {**info, "effect": None, "top": [], "histogram": [],
                    "note": f"needs {model.cfg.calibration_min} headlines to calibrate (has {n})"}
        recent, hours = self._recent(now)
        r = model.relevance([x[1] for x in recent], [x[2] for x in recent])
        top = []
        for i in np.argsort(-r)[:top_n]:
            if r[i] <= 0:
                break
            v = recent[i][2]
            z = float(model.z(model.similarity(v[None, :]))[0]) if model.semantic and v is not None else None
            top.append({"headline": recent[i][1], "relevance": float(r[i]), "z": z})
        hist = np.histogram(r, bins=[0, 0.1, 0.5, 0.9, 1.0001])[0].tolist() if len(r) else []
        return {**info, "effect": model.effect(r, np.asarray([x[3] for x in recent]), hours),
                "top": top, "histogram": hist}

    def _recent(self, now: datetime | None, hours: float = 24.0) -> tuple[list, float]:
        """Retained headlines of the last ``hours`` before ``now`` (default: the newest one), and
        the hours they actually span (shorter right after a start)."""
        if not self._items_t:
            return [], hours
        end = now or self._items_t[-1]
        i = bisect.bisect_right(self._items_t, end - timedelta(hours=hours))
        j = bisect.bisect_right(self._items_t, end)
        span = (end - self._items_t[i]).total_seconds() / 3600 if i < j else hours
        return self._items[i:j], min(hours, max(1.0, span))

    def focus_info(self, now: datetime | None = None) -> dict[str, Any]:
        f = self.focus
        if f is None:
            return {"text": "", "phrases": [], "background_weight": self.s.focus.background_weight}
        recent, hours = self._recent(now)
        key = (id(f), len(self._items), recent[-1][0] if recent else None)
        if self._effect_cache is None or self._effect_cache[0] != key:  # status is polled every second
            r = np.asarray([self._rel.get(x[0], 0.0) for x in recent])
            eff = f.effect(r, np.asarray([x[3] for x in recent]), hours) if f.calibrated else None
            self._effect_cache = (key, eff or {})
        return {**f.info(), "effect": self._effect_cache[1] or None}

    def model_cost(self) -> dict[str, Any]:
        """AI model time per headline: from real model runs (cache misses) when there were any, else
        the last batch (which may be mostly cache lookups, and says so)."""
        runs = [b for b in (self.sentiment, self.embedder) if getattr(b, "scored", 0)]
        if runs:
            n = max(b.scored for b in runs)  # type: ignore[attr-defined]
            ms = sum(b.score_s / b.scored for b in runs) * 1000  # type: ignore[attr-defined]
            return {"ms_per_headline": ms, "headlines": n, "source": "model"}
        if self.model_timing:
            return {**self.model_timing, "source": "batch"}
        return {}

    def trending_words(self, t: datetime | None = None, top: int = 8) -> list[dict[str, Any]]:
        """Words unusually common in the last hour vs the week before (trending.py), each headline
        weighted as in the score. An explanation for people; the score never uses it."""
        if not self._items_t:
            return []
        end = t or self._items_t[-1]
        recent_from = end - timedelta(minutes=self.s.drift.recent_window_minutes)
        i0, i1, i2 = (bisect.bisect_right(self._items_t, x) for x in (
            end - timedelta(days=self.s.drift.reference_window_days), recent_from, end))
        recent = [(x[4], terms(x[1])[1], self.weight(x[0]), x[5]) for x in self._items[i1:i2]]
        reference = [(x[4], self.weight(x[0])) for x in self._items[i0:i1]]
        return trending(recent, reference, top=top)

    def compute(self, t: datetime) -> RiskSnapshot:
        if self._needs_rebuild:
            self.rebuild_baselines(t)
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
        cutoff = t - timedelta(days=self.s.intensity.lookback_days + 2)  # the longest any tracker keeps
        i = bisect.bisect_left(self._items_t, cutoff)
        if i > 256:
            for hid, *_ in self._items[:i]:
                self._rel.pop(hid, None)
            del self._items_t[:i]
            del self._items[:i]
