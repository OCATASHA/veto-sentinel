"""Monitor: ingestion -> features -> composite -> change detection -> state machine -> store.

The monitor is clock-agnostic (live ``SystemClock`` or replay ``SimClock``) and transport-
agnostic: it exposes ``status()`` (a full snapshot for the bus/dashboard) and calls
``on_change`` listeners whenever the published state changes or a control event occurs.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vetosentinel.config import Settings
from vetosentinel.detect.cusum import CusumDetector
from vetosentinel.engine.state_machine import EngineInput, StateMachine
from vetosentinel.features.pipeline import FeaturePipeline
from vetosentinel.ingest.base import AdapterRunner, FetchResult
from vetosentinel.ingest.dedup import Deduplicator
from vetosentinel.ingest.normalize import NormalizationError, normalize_item
from vetosentinel.models import ChangePointResult, DecisionRecord, Headline, RiskSnapshot, RiskState
from vetosentinel.store import Store
from vetosentinel.timeutil import Clock, utcnow

log = logging.getLogger(__name__)


@dataclass
class MonitorStatus:
    run_id: str
    mode: str
    data_origin: str
    state: RiskState
    snapshot: RiskSnapshot | None
    decision: DecisionRecord | None
    reason: str
    top_headlines: list[dict[str, str]]
    implementations: dict[str, dict]
    model_notes: list[str]
    adapters: list[dict[str, Any]]
    counters: dict[str, int]
    flatten: dict[str, Any] | None
    control: dict[str, Any] | None = None
    focus: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class Monitor:
    def __init__(self, settings: Settings, store: Store, pipeline: FeaturePipeline,
                 adapters: list[AdapterRunner], clock: Clock, *, mode: str, data_origin: str,
                 run_id: str | None = None, model_notes: list[str] | None = None,
                 coverage_start: datetime | None = None, fixture_meta: dict | None = None,
                 fixture_path: str | None = None):
        self.s = settings
        self.store = store
        self.pipeline = pipeline
        self.adapters = adapters
        self.clock = clock
        self.mode = mode
        self.data_origin = data_origin
        self.run_id = run_id or f"{mode}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        self.model_notes = model_notes or []
        self.sm = StateMachine(settings.state_machine)
        self.detector = CusumDetector(settings.cusum)
        self.dedup = Deduplicator(timedelta(days=settings.ingest.dedup_ttl_days))
        self.lock = threading.RLock()
        self.listeners: list[Callable[[str], None]] = []
        self.counters = {"ticks": 0, "headlines": 0, "duplicates": 0, "malformed": 0,
                         "skipped_language": 0, "transitions": 0}
        self._last_snapshot: RiskSnapshot | None = None
        self._last_decision: DecisionRecord | None = None
        self._last_control: dict[str, Any] | None = None
        self._headline_cache: dict[str, Headline] = {}
        self._pending_flatten: dict[str, dict[str, Any]] = {}
        self._last_ingest_success: datetime | None = None
        self._outage_start: datetime | None = None
        self._last_eval: datetime | None = None
        self._cp_t: datetime | None = None  # news time of the last CUSUM update
        self._focus_meta: dict[str, Any] = {}
        self._trend_cache: tuple[Any, list[dict[str, Any]], float] | None = None
        self.tick_ms: float | None = None  # wall time of the last decision (speed budget)
        if coverage_start is not None:
            pipeline.intensity.set_coverage_start(coverage_start)
        store.start_run(self.run_id, mode, data_origin, settings.model_dump(mode="json"),
                        pipeline.implementations, settings.parameter_status, fixture_path, fixture_meta)

    # ------------------------------------------------------------------ ingestion
    def ingest_result(self, runner: AdapterRunner, res: FetchResult, now: datetime) -> int:
        """Normalise, dedupe, score and store one fetch result. Returns the number of new headlines."""
        new: list[Headline] = []
        dups: list[tuple[Headline, str, str]] = []
        malformed = res.malformed
        skipped_lang = 0
        if res.ok:
            for item in res.items:
                try:
                    h = normalize_item(
                        item, provider=runner.adapter.name, data_origin=runner.adapter.data_origin,
                        fetched_at=now, source_weights=self.s.ingest.source_weights,
                        default_weight=self.s.ingest.default_source_weight,
                        future_skew=timedelta(seconds=self.s.ingest.future_skew_seconds))
                except (NormalizationError, ValueError):
                    malformed += 1
                    continue
                if h.language not in self.s.ingest.languages:
                    skipped_lang += 1
                    continue
                d = self.dedup.check_and_add(h)
                if d.duplicate:
                    dups.append((h, d.original_id or "", d.reason))
                else:
                    new.append(h)
        scores = self.pipeline.ingest(new)
        for h in new:
            self._headline_cache[h.headline_id] = h
        self.store.add_headlines(self.run_id, [
            (h, scores.get(h.headline_id), self.pipeline.sentiment.implementation,
             self.pipeline.embedder.implementation) for h in new])
        self.store.add_duplicates(self.run_id, dups)
        self.store.add_ingest_log(self.run_id, now, runner.adapter.name, res.ok, len(res.items), len(new),
                                  len(dups), malformed, res.error)
        self.counters["headlines"] += len(new)
        self.counters["duplicates"] += len(dups)
        self.counters["malformed"] += malformed
        self.counters["skipped_language"] += skipped_lang
        if res.ok:
            if self._last_ingest_success is None:
                # live coverage begins at the first successful fetch
                self.pipeline.intensity.set_coverage_start(now)
            if self._outage_start is not None:
                self.pipeline.intensity.mark_gap(self._outage_start, now)
                self._outage_start = None
            self._last_ingest_success = now
            # a backfill declares exactly which past period it covers, and its holes
            if res.coverage_start is not None:
                self.pipeline.intensity.set_coverage_start(res.coverage_start)
            for gap_start, gap_end in res.gaps:
                self.pipeline.intensity.mark_gap(gap_start, gap_end)
        return len(new)

    def _ingest(self, now: datetime) -> None:
        any_attempt = False
        for runner in self.adapters:
            if not runner.due(now):
                continue
            any_attempt = True
            self.ingest_result(runner, runner.run(now), now)
        if not any_attempt:
            return
        stale = timedelta(seconds=self.s.ingest.stale_after_seconds)
        if self._last_ingest_success is not None and now - self._last_ingest_success > stale \
                and self._outage_start is None:
            self._outage_start = self._last_ingest_success

    def _ingest_health(self, now: datetime) -> list[str]:
        if not self.adapters:
            return ["no news adapters configured"]
        if self._last_ingest_success is None:
            return ["no successful news fetch yet"]
        age = now - self._last_ingest_success
        if age > timedelta(seconds=self.s.ingest.stale_after_seconds):
            return [f"news ingestion stale: last successful fetch {age} ago"]
        return []

    def cooldown_status(self) -> dict[str, Any] | None:
        """Current cooling-off progress on the monitor's clock (exact to the second in live mode)."""
        with self.lock:
            return self.sm.cooldown_status(self.clock.now())

    def exit_checklist(self) -> dict[str, Any] | None:
        """What the next relax step still needs (StateMachine.exit_checklist), for operators."""
        with self.lock:
            return self.sm.exit_checklist(self.clock.now())

    def eval_time(self, now: datetime) -> datetime:
        """Time the news is assessed at: now, or - if a source reports how far its data is complete
        (GDELT publishes with a lag) - that point, so late news is never mistaken for a quiet hour.
        Never moves backwards."""
        through = [t for r in self.adapters if (t := getattr(r.adapter, "data_through", None)) is not None]
        t = min(now, max(through)) if through else now
        return max(t, self._last_eval) if self._last_eval is not None else t

    def warm_up(self, start: datetime, end: datetime, step: timedelta = timedelta(minutes=10)) -> int:
        """Replay feature computation over already-ingested history so the rolling baselines are
        ready at the first live tick. Touches no state machine, store or bus. Returns the step count."""
        n = 0
        with self.lock:
            self.pipeline.reset_baselines()  # the warm-up builds them from scratch
            t = start
            while t < end:
                self.pipeline.compute(t)
                t += step
                n += 1
        return n

    # ------------------------------------------------------------------ tick
    def tick(self) -> DecisionRecord:
        with self.lock:
            now = self.clock.now()
            self._ingest(now)
            t0 = time.perf_counter()  # the decision itself: features, score, change point, state, store
            t_eval = self.eval_time(now)
            self._last_eval = t_eval
            snap = self.pipeline.compute(t_eval)
            ingest_problems = self._ingest_health(now)
            lag = now - t_eval
            if lag > timedelta(minutes=self.s.ingest.max_data_lag_minutes):
                ingest_problems.append(f"news data is {int(lag.total_seconds() // 60)} min behind "
                                       f"(complete up to {t_eval:%H:%M} UTC)")
            if ingest_problems:
                snap = snap.model_copy(update={
                    "monitor_ready": False, "not_ready_reasons": snap.not_ready_reasons + ingest_problems})
            cp = self._changepoint(t_eval, snap.risk_score if snap.monitor_ready else None)
            comps = {k: (v.z if v.available else None) for k, v in snap.components.items()}
            health = {"implementations": self.pipeline.implementations,
                      "model_errors": {"sentiment": self.pipeline.sentiment_errors,
                                       "embedding": self.pipeline.embed_errors,
                                       "last": self.pipeline.last_model_error},
                      "adapters": [self._adapter_dict(r) for r in self.adapters]}
            decision = self.sm.step(EngineInput(
                as_of=t_eval, risk_score=snap.risk_score, monitor_ready=snap.monitor_ready, components=comps,
                not_ready_reasons=snap.not_ready_reasons, degraded=snap.degraded,
                degraded_reasons=snap.degraded_reasons, changepoint=cp,
                headline_ids=snap.top_headline_ids, health=health, now=now,
                news_seen=self.counters["headlines"] if self.mode == "live" else None))
            self.store.add_tick(self.run_id, snap, decision, cp.statistic, cp.alarm_active)
            self._last_snapshot, self._last_decision = snap, decision
            self.counters["ticks"] += 1
            self.tick_ms = (time.perf_counter() - t0) * 1000
            self.pipeline.prune(t_eval)
            if len(self._headline_cache) > 20_000:
                self._prune_headline_cache(now)
            if decision.changed:
                self.counters["transitions"] += 1
                log.info("STATE %s -> %s [%s] %s", decision.prev_state, decision.new_state,
                         decision.rule, decision.reason)
        if decision.changed:
            self._notify("STATE")
        return decision

    def _changepoint(self, t: datetime, x: float | None) -> ChangePointResult:
        """Update CUSUM once per replay tick (5 min) of news time. Live news time stands still
        between batches and then jumps, so updating on every 60 s tick would re-add the same
        reading many times; a jump of several steps adds it once per step (at most an hour)."""
        grid = timedelta(minutes=self.s.replay.tick_minutes)
        if self._cp_t is None:
            steps = 1
        else:
            steps = min(int((t - self._cp_t) / grid), 12)
            if steps <= 0:
                return self.detector.hold(t)
        cp = self.detector.update(t, x)
        for _ in range(steps - 1):
            if cp.alarm:
                break
            cp = self.detector.update(t, x)
        self._cp_t = t if self._cp_t is None or steps == 12 else self._cp_t + steps * grid
        return cp

    def _prune_headline_cache(self, now: datetime) -> None:
        cutoff = now - timedelta(days=1)
        self._headline_cache = {k: h for k, h in self._headline_cache.items() if h.event_time >= cutoff}

    # ------------------------------------------------------------------ focus ("Optimize for")
    def set_focus(self, text: str, background: float | None, operator: str) -> dict[str, Any]:
        """Weight news by relevance to ``text`` ("" = whole market). Rebuilds the baselines with
        the new weights, logs the change, and re-assesses immediately (escalation is immediate;
        relaxing still needs the usual calm period)."""
        with self.lock:
            until = self._last_eval
            t0 = utcnow()
            info = self.pipeline.set_focus(text, background, rebuild_until=until)
            self._focus_meta = {"set_by": operator, "set_at": utcnow().isoformat(),
                                "as_of": until.isoformat() if until else None,
                                "recompute_seconds": round((utcnow() - t0).total_seconds(), 2)}
            self.store.add_control_event(self.run_id, utcnow(), "FOCUS_SET", operator,
                                         {"text": info.get("text", ""),
                                          "background_weight": self.s.focus.background_weight})
            self._last_control = {"event": "FOCUS_SET", "operator": operator, "at": utcnow().isoformat(),
                                  "text": info.get("text", ""), "automatic_execution": False}
            log.info("focus set by %s: %r (background %.2f)", operator, info.get("text") or "whole market",
                     self.s.focus.background_weight)
        if until is not None:
            self.tick()
        self._notify("CONTROL")
        return self.focus_info()

    def preview_focus(self, text: str, background: float | None) -> dict[str, Any]:
        with self.lock:
            return self.pipeline.preview_focus(text, background, self._last_eval)

    def focus_info(self) -> dict[str, Any]:
        with self.lock:
            return {**self.pipeline.focus_info(self._last_eval), **self._focus_meta}

    # ------------------------------------------------------------------ trending words (explanation only)
    def trending(self) -> list[dict[str, Any]]:
        """Trending words as of the current assessment. Cached per news time and weights, and
        recomputed at most every 2 s while a fast replay moves the news time on."""
        with self.lock:
            key = (self._last_eval, self.pipeline.weights_version)
            c, now = self._trend_cache, time.monotonic()
            if c is not None and (c[0] == key or (c[0][1] == key[1] and now - c[2] < 2.0)):
                return c[1]
            out = self.pipeline.trending_words(self._last_eval)
            self._trend_cache = (key, out, now)
            return out

    def trending_at(self, t: datetime) -> list[dict[str, Any]]:
        with self.lock:
            return self.pipeline.trending_words(t)

    # ------------------------------------------------------------------ operator FLATTEN (two-step)
    def request_flatten(self, operator: str, reason: str) -> dict[str, Any]:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required")
        token = secrets.token_hex(3).upper()
        expires = utcnow() + timedelta(minutes=2)
        with self.lock:
            self._pending_flatten = {token: {"operator": operator, "reason": reason, "expires": expires}}
            self.store.add_control_event(self.run_id, utcnow(), "FLATTEN_REQUESTED", operator,
                                         {"reason": reason, "expires": expires.isoformat()})
        return {"confirmation_code": token, "expires": expires.isoformat(),
                "note": "Confirm with the same operator within 2 minutes. FLATTEN is an instruction "
                        "for a separate human-controlled process; Veto Sentinel never trades."}

    def confirm_flatten(self, operator: str, code: str) -> DecisionRecord:
        with self.lock:
            p = self._pending_flatten.get(code.strip().upper())
            if p is None or p["expires"] < utcnow() or p["operator"] != operator:
                self.store.add_control_event(self.run_id, utcnow(), "FLATTEN_CONFIRM_REJECTED", operator,
                                             {"code": code})
                raise PermissionError("invalid, expired, or mismatched confirmation code")
            self._pending_flatten.clear()
            d = self.sm.force_flatten(self.clock.now(), operator, p["reason"])
            self._record_control(d, "FLATTEN_CONFIRMED", operator, {"reason": p["reason"]})
        self._notify("CONTROL")
        return d

    def clear_flatten(self, operator: str, reason: str) -> DecisionRecord:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required")
        with self.lock:
            d = self.sm.clear_flatten(self.clock.now(), operator, reason)
            self._record_control(d, "FLATTEN_CLEARED", operator, {"reason": reason})
        self._notify("CONTROL")
        return d

    def _record_control(self, d: DecisionRecord, kind: str, operator: str, detail: dict) -> None:
        self.store.add_decision(self.run_id, d)
        self.store.add_control_event(self.run_id, d.wall_ts, kind, operator, detail)
        self._last_decision = d
        self._last_control = {"event": kind, "operator": operator, "at": d.wall_ts.isoformat(), **detail,
                              "automatic_execution": False}
        self.counters["transitions"] += 1

    # ------------------------------------------------------------------ status
    def _notify(self, kind: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(kind)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    @staticmethod
    def _adapter_dict(r: AdapterRunner) -> dict[str, Any]:
        h = r.health
        return {"name": h.name, "data_origin": h.data_origin,
                "last_success": h.last_success.isoformat() if h.last_success else None,
                "last_attempt": h.last_attempt.isoformat() if h.last_attempt else None,
                "last_error": h.last_error, "consecutive_failures": h.consecutive_failures,
                "total_items": h.total_items, "total_malformed": h.total_malformed,
                "poll_seconds": r.poll.total_seconds(),
                "data_through": (dt.isoformat() if (dt := getattr(r.adapter, "data_through", None)) else None),
                "late_files": len(getattr(r.adapter, "pending", ())) + len(getattr(r.adapter, "late", ()))}

    def status(self) -> MonitorStatus:
        with self.lock:
            snap, d = self._last_snapshot, self._last_decision
            top = []
            if snap is not None:
                for hid in snap.top_headline_ids[: self.s.bus.top_headlines]:
                    h = self._headline_cache.get(hid)
                    if h is not None:
                        top.append({"headline_id": hid, "headline": h.headline, "source": h.source})
            focus = self.focus_info()
            return MonitorStatus(
                run_id=self.run_id, mode=self.mode, data_origin=self.data_origin, state=self.sm.state,
                snapshot=snap, decision=d,
                reason=d.reason if d else "monitor starting: no assessment yet (fail-safe HALT)",
                top_headlines=top, implementations=self.pipeline.implementations,
                model_notes=self.model_notes, adapters=[self._adapter_dict(r) for r in self.adapters],
                counters=dict(self.counters), flatten=self.sm.flatten_info, control=self._last_control,
                focus=focus)
