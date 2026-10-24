"""Deterministic risk-state machine with hysteresis and cooling-off.

Rules, evaluated once per tick in this order (see docs/STATE_MACHINE.md):

1. ``operator_flatten_hold`` - FLATTEN is sticky. Only an operator ``clear_flatten`` leaves it
   (to HALT). Automatic logic never enters or leaves FLATTEN.
2. ``monitor_not_ready``    - warm-up incomplete, ingestion stale, or required data missing
   -> HALT immediately.
3. Escalation target = max(score level, change-point level, degraded floor):
     score level:   R >= halt_enter -> HALT; R >= caution_enter -> CAUTION; else NORMAL
     change point:  active CUSUM alarm -> at least CAUTION; HALT if R >= caution_enter
     degraded:      any non-required component unavailable -> at least ``degraded_floor``
   If the target is more severe than the current state -> escalate immediately.
4. De-escalation is one level at a time and only after the exit condition has held
   continuously for ``cooldown_minutes`` on the monitor's clock (simulated time in a replay, real
   time live - live news arrives late and in batches, so news time would make the wait
   unpredictable), and new news has been assessed since it started (live: new headlines arrived;
   replay: news time moved on), so a stalled feed never counts as calm:
     HALT -> CAUTION    requires R < halt_exit and escalation target <= CAUTION
     CAUTION -> NORMAL  requires R < caution_exit and escalation target == NORMAL
   Any interruption of the exit condition resets the cooling-off timer.
5. Otherwise hold.

The machine starts in HALT: there is no evidence of safety until the monitor is ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vetosentinel.config import StateMachineConfig
from vetosentinel.models import ChangePointResult, DecisionRecord, RiskState
from vetosentinel.timeutil import utcnow


@dataclass
class EngineInput:
    as_of: datetime
    risk_score: float | None
    monitor_ready: bool
    components: dict[str, float | None] = field(default_factory=dict)
    not_ready_reasons: list[str] = field(default_factory=list)
    degraded: bool = False
    degraded_reasons: list[str] = field(default_factory=list)
    changepoint: ChangePointResult | None = None
    headline_ids: list[str] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    now: datetime | None = None  # monitor clock that times the cooling-off; defaults to as_of
    news_seen: int | None = None  # live: headlines ingested so far (tells new news from a quiet feed)


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


class StateMachine:
    def __init__(self, cfg: StateMachineConfig, initial: RiskState = RiskState.HALT):
        self.cfg = cfg
        self.state = initial
        self._exit_since: datetime | None = None  # monitor-clock start of the current cooling-off
        self._exit_news: float | None = None  # news marker (see _news_marker) when it started
        self._last_news: float | None = None
        self._exit_to: RiskState | None = None  # level the current cooling-off would relax to
        self._last_as_of: datetime | None = None
        self.flatten_info: dict[str, Any] | None = None

    @property
    def cooldown(self) -> timedelta:
        return timedelta(minutes=self.cfg.cooldown_minutes)

    # ------------------------------------------------------------------ automatic path
    def step(self, inp: EngineInput) -> DecisionRecord:
        if self._last_as_of is not None and inp.as_of < self._last_as_of:
            raise ValueError(f"as_of went backwards: {inp.as_of} < {self._last_as_of}")
        self._last_as_of = inp.as_of
        self._last_news = self._news_marker(inp)
        prev = self.state

        if self.state is RiskState.FLATTEN:
            return self._record(inp, prev, RiskState.FLATTEN, "operator_flatten_hold",
                                "FLATTEN is operator-controlled; awaiting human clear_flatten")

        if not inp.monitor_ready or inp.risk_score is None:
            reasons = inp.not_ready_reasons or (["risk score unavailable"] if inp.risk_score is None else [])
            self._exit_since = None
            return self._transition(inp, prev, RiskState.HALT, "monitor_not_ready",
                                    "Monitor not ready: " + "; ".join(reasons))

        score = inp.risk_score
        target, why = self._escalation_target(inp, score)

        if target.severity > self.state.severity:
            self._exit_since = None
            return self._transition(inp, prev, target, "escalate", why)

        if target.severity < self.state.severity:
            nxt, exit_ok, exit_why = self._exit_condition(target, score)
            if not exit_ok:
                self._exit_since = None
                return self._record(inp, prev, prev, "hold",
                                    f"Holding {prev}: {exit_why}; {why}")
            now = inp.now or inp.as_of
            if self._exit_since is None:
                self._exit_since, self._exit_news = now, self._last_news
            self._exit_to = nxt
            elapsed = now - self._exit_since
            if elapsed >= self.cooldown and self._has_newer_news():
                self._exit_since = None
                return self._transition(
                    inp, prev, nxt, "deescalate_after_cooldown",
                    f"{exit_why} for {elapsed} (cooldown {self.cooldown}); stepping {prev} -> {nxt}")
            if elapsed >= self.cooldown:
                return self._record(inp, prev, prev, "cooldown_pending",
                                    f"{exit_why} for {elapsed}; waiting for newer news before {nxt}")
            remaining = self.cooldown - elapsed
            return self._record(inp, prev, prev, "cooldown_pending",
                                f"{exit_why}; cooling off, {remaining} remaining before {nxt}")

        self._exit_since = None
        return self._record(inp, prev, prev, "hold", why)

    def _escalation_target(self, inp: EngineInput, score: float) -> tuple[RiskState, str]:
        c = self.cfg
        if score >= c.halt_enter:
            level, why = RiskState.HALT, f"R={score:.2f} >= halt_enter {c.halt_enter}"
        elif score >= c.caution_enter:
            level, why = RiskState.CAUTION, f"R={score:.2f} >= caution_enter {c.caution_enter}"
        else:
            level, why = RiskState.NORMAL, f"R={score:.2f} < caution_enter {c.caution_enter}"
        parts = [why]

        cp = inp.changepoint
        if c.changepoint_escalates and cp is not None and cp.alarm_active:
            cp_level = RiskState.HALT if score >= c.caution_enter else RiskState.CAUTION
            parts.append(f"{cp.detector} change-point active (S={cp.statistic:.2f}, h={cp.threshold})"
                         f" -> at least {cp_level}")
            level = RiskState.max(level, cp_level)

        if inp.degraded:
            floor = RiskState(c.degraded_floor)
            parts.append(f"degraded data ({'; '.join(inp.degraded_reasons)}) -> floor {floor}")
            level = RiskState.max(level, floor)

        comps = ", ".join(f"{k}_z={_fmt(v)}" for k, v in inp.components.items())
        if comps:
            parts.append(comps)
        return level, " | ".join(parts)

    def _exit_condition(self, target: RiskState, score: float) -> tuple[RiskState, bool, str]:
        c = self.cfg
        if self.state is RiskState.HALT:
            ok = score < c.halt_exit and target.severity <= RiskState.CAUTION.severity
            return RiskState.CAUTION, ok, (
                f"R={score:.2f} < halt_exit {c.halt_exit}" if ok
                else f"exit condition for HALT not met (R={score:.2f}, halt_exit {c.halt_exit}, target {target})")
        # CAUTION -> NORMAL
        ok = score < c.caution_exit and target is RiskState.NORMAL
        return RiskState.NORMAL, ok, (
            f"R={score:.2f} < caution_exit {c.caution_exit}" if ok
            else f"exit condition for CAUTION not met (R={score:.2f}, caution_exit {c.caution_exit}, target {target})")

    @staticmethod
    def _news_marker(inp: EngineInput) -> float:
        """Grows when new news has been assessed: headline count if given (live), else news time."""
        return float(inp.news_seen) if inp.news_seen is not None else inp.as_of.timestamp()

    def _has_newer_news(self) -> bool:
        return self._exit_news is not None and self._last_news is not None and self._last_news > self._exit_news

    def cooldown_status(self, now: datetime) -> dict[str, Any] | None:
        """Progress of the current cooling-off period at ``now`` on the monitor's clock, or None."""
        if self._exit_since is None or self._exit_to is None or self.state is RiskState.FLATTEN:
            return None
        elapsed = now - self._exit_since
        remaining = max(timedelta(0), self.cooldown - elapsed)
        return {"next_state": str(self._exit_to), "started": self._exit_since.isoformat(),
                "ends": (self._exit_since + self.cooldown).isoformat(),
                "elapsed_s": elapsed.total_seconds(), "remaining_s": remaining.total_seconds(),
                "total_s": self.cooldown.total_seconds(),
                # no new news assessed since the start yet: it won't relax until there is
                "needs_newer_news": not self._has_newer_news()}

    # ------------------------------------------------------------------ operator path
    def force_flatten(self, as_of: datetime, operator: str, reason: str) -> DecisionRecord:
        """Human-confirmed FLATTEN. Records an instruction only; never executes trades."""
        prev = self.state
        self.flatten_info = {"operator": operator, "reason": reason, "at": as_of.isoformat()}
        self.state = RiskState.FLATTEN
        self._exit_since = None
        return self._make(EngineInput(as_of=as_of, risk_score=None, monitor_ready=True), prev,
                          RiskState.FLATTEN, "operator_flatten",
                          f"Operator {operator!r} confirmed FLATTEN: {reason}. "
                          "Instruction only - requires a separate human-controlled process.")

    def clear_flatten(self, as_of: datetime, operator: str, reason: str) -> DecisionRecord:
        if self.state is not RiskState.FLATTEN:
            raise ValueError("not in FLATTEN")
        prev = self.state
        self.state = RiskState.HALT  # resume via normal, cautious de-escalation
        self.flatten_info = None
        self._exit_since = None
        return self._make(EngineInput(as_of=as_of, risk_score=None, monitor_ready=True), prev,
                          RiskState.HALT, "operator_clear_flatten",
                          f"Operator {operator!r} cleared FLATTEN: {reason}. Resuming at HALT.")

    # ------------------------------------------------------------------ helpers
    def _transition(self, inp: EngineInput, prev: RiskState, new: RiskState, rule: str,
                    reason: str) -> DecisionRecord:
        self.state = new
        return self._make(inp, prev, new, rule if new is not prev else "hold", reason)

    def _record(self, inp: EngineInput, prev: RiskState, new: RiskState, rule: str,
                reason: str) -> DecisionRecord:
        return self._make(inp, prev, new, rule, reason)

    def _make(self, inp: EngineInput, prev: RiskState, new: RiskState, rule: str,
              reason: str) -> DecisionRecord:
        return DecisionRecord(
            as_of=inp.as_of, wall_ts=utcnow(), prev_state=prev, new_state=new,
            changed=prev is not new, rule=rule, reason=reason, risk_score=inp.risk_score,
            components=dict(inp.components), changepoint=inp.changepoint,
            headline_ids=list(inp.headline_ids),
            health={**inp.health, "monitor_ready": inp.monitor_ready,
                    "not_ready_reasons": inp.not_ready_reasons,
                    "degraded": inp.degraded, "degraded_reasons": inp.degraded_reasons},
        )
