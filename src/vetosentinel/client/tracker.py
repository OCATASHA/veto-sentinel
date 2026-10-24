"""Client-side fail-safe state tracker (pure logic, injectable clocks, no sockets).

Recovery policy (documented contract):
  * Startup state is HALT ("fault: startup").
  * Any of the following puts the client in FAULT and sets local state to HALT immediately:
      - no valid message within the heartbeat timeout (watchdog);
      - an undecodable / schema-invalid / oversized message;
      - a sequence gap, duplicate, or out-of-order (replayed) sequence number;
      - a publisher restart (publisher_id changed);
      - a message timestamp older than ``max_message_age_ms`` or further in the future than
        ``max_future_skew_ms`` (relative to the local UTC clock);
      - a message reporting ``monitor_ready=false``.
  * While in FAULT the local state is max(HALT, published) (FLATTEN still propagates).
  * The client leaves FAULT only after ``recovery_snapshots_required`` consecutive valid,
    fresh, in-sequence messages from a ready monitor (each message is a full snapshot). It
    then adopts the published state.
  * Outside FAULT the published state is adopted immediately (escalations and the
    monitor's own hysteresis-controlled de-escalations).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from vetosentinel.bus.schema import BusMessage, decode
from vetosentinel.models import RiskState


class Fault(StrEnum):
    STARTUP = "startup"
    STALE_HEARTBEAT = "stale_heartbeat"
    INVALID_MESSAGE = "invalid_message"
    SEQUENCE_GAP = "sequence_gap"
    DUPLICATE = "duplicate_seq"
    OUT_OF_ORDER = "out_of_order_seq"
    PUBLISHER_RESTART = "publisher_restart"
    STALE_TIMESTAMP = "stale_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    MONITOR_NOT_READY = "monitor_not_ready"


@dataclass
class ClientConfig:
    heartbeat_timeout_ms: int = 3000
    max_message_age_ms: int = 3000
    max_future_skew_ms: int = 1000
    recovery_snapshots_required: int = 2
    # Use the smaller of our timeout and the one advertised by the monitor.
    honor_advertised_timeout: bool = True
    # CAUTION position limit as a fraction of the strategy's normal limit.
    caution_position_fraction: float = 0.25

    def __post_init__(self) -> None:
        if self.heartbeat_timeout_ms <= 0 or self.max_message_age_ms <= 0:
            raise ValueError("timeouts must be positive")
        if self.recovery_snapshots_required < 1:
            raise ValueError("recovery_snapshots_required must be >= 1")
        if not 0.0 <= self.caution_position_fraction <= 1.0:
            raise ValueError("caution_position_fraction must be in [0, 1]")


@dataclass(frozen=True)
class Transition:
    old: RiskState
    new: RiskState
    reason: str


@dataclass
class TrackerStats:
    received: int = 0
    valid: int = 0
    faults: dict[str, int] = field(default_factory=lambda: {f.value: 0 for f in Fault})
    recoveries: int = 0


class StateTracker:
    def __init__(self, cfg: ClientConfig | None = None):
        self.cfg = cfg or ClientConfig()
        self._lock = threading.Lock()
        # Hot-path fields: plain attribute reads are atomic in CPython.
        self.local_state: RiskState = RiskState.HALT
        self.last_valid_mono: float | None = None
        self.timeout_s: float = self.cfg.heartbeat_timeout_ms / 1000.0
        # Guarded by _lock
        self.in_fault = True
        self.fault: Fault | None = Fault.STARTUP
        self.fault_detail = "startup: no valid message received yet"
        self.publisher_id: str | None = None
        self.last_seq: int | None = None
        self.consecutive_good = 0
        self.last_message: BusMessage | None = None
        self.stats = TrackerStats()
        self.stats.faults[Fault.STARTUP.value] = 1
        self.events: deque[tuple[float, str]] = deque(maxlen=200)

    # ------------------------------------------------------------------ hot-path helpers
    def is_stale(self, now_mono: float) -> bool:
        lv = self.last_valid_mono
        return lv is None or (now_mono - lv) > self.timeout_s

    def effective_state(self, now_mono: float) -> RiskState:
        """Local state, forced to (at least) HALT if the heartbeat is stale. Never blocks."""
        st = self.local_state
        if self.is_stale(now_mono) and st.severity < RiskState.HALT.severity:
            return RiskState.HALT
        return st

    # ------------------------------------------------------------------ transitions
    def _set_local(self, new: RiskState, reason: str, out: list[Transition]) -> None:
        old = self.local_state
        if new is not old:
            self.local_state = new
            out.append(Transition(old, new, reason))

    def _enter_fault(self, fault: Fault, detail: str, now_mono: float, out: list[Transition]) -> None:
        if not self.in_fault or self.fault is not fault:
            self.stats.faults[fault.value] += 1
            self.events.append((now_mono, f"FAULT {fault.value}: {detail}"))
        self.in_fault = True
        self.fault = fault
        self.fault_detail = detail
        self.consecutive_good = 0
        # FLATTEN is stricter than HALT (operator instruction), so a fault never downgrades it.
        published = self.last_message.state if self.last_message else RiskState.HALT
        target = RiskState.FLATTEN if published is RiskState.FLATTEN else RiskState.HALT
        self._set_local(target, f"fail-safe: {fault.value}: {detail}", out)

    def check_watchdog(self, now_mono: float) -> list[Transition]:
        out: list[Transition] = []
        with self._lock:
            if self.is_stale(now_mono):
                if not (self.in_fault and self.fault in (Fault.STALE_HEARTBEAT, Fault.STARTUP)):
                    age = "never" if self.last_valid_mono is None else f"{now_mono - self.last_valid_mono:.2f}s"
                    self._enter_fault(Fault.STALE_HEARTBEAT,
                                      f"no valid message for {age} (timeout {self.timeout_s:.2f}s)", now_mono, out)
                elif self.local_state.severity < RiskState.HALT.severity:
                    self._set_local(RiskState.HALT, "fail-safe: heartbeat stale", out)
        return out

    def on_payload(self, payload: bytes, now_mono: float, now_wall: datetime) -> list[Transition]:
        out: list[Transition] = []
        with self._lock:
            self.stats.received += 1
            try:
                msg = decode(payload)
            except ValueError as e:
                self._enter_fault(Fault.INVALID_MESSAGE, str(e)[:300], now_mono, out)
                return out

            # --- sequencing
            if self.publisher_id is not None and msg.publisher_id != self.publisher_id:
                self._enter_fault(Fault.PUBLISHER_RESTART,
                                  f"publisher_id {self.publisher_id} -> {msg.publisher_id}", now_mono, out)
                self.last_seq = None
            self.publisher_id = msg.publisher_id
            gap = False
            if self.last_seq is not None:
                if msg.seq == self.last_seq:
                    self._enter_fault(Fault.DUPLICATE, f"seq {msg.seq} repeated", now_mono, out)
                    return out
                if msg.seq < self.last_seq:
                    self._enter_fault(Fault.OUT_OF_ORDER, f"seq {msg.seq} after {self.last_seq}", now_mono, out)
                    return out
                if msg.seq > self.last_seq + 1:
                    self._enter_fault(Fault.SEQUENCE_GAP,
                                      f"missed {msg.seq - self.last_seq - 1} message(s) "
                                      f"({self.last_seq} -> {msg.seq})", now_mono, out)
                    gap = True
            self.last_seq = msg.seq

            # --- freshness
            age_ms = (now_wall - msg.ts).total_seconds() * 1000.0
            if age_ms > self.cfg.max_message_age_ms:
                self._enter_fault(Fault.STALE_TIMESTAMP, f"message is {age_ms:.0f}ms old", now_mono, out)
                return out
            if -age_ms > self.cfg.max_future_skew_ms:
                self._enter_fault(Fault.FUTURE_TIMESTAMP, f"message is {-age_ms:.0f}ms in the future",
                                  now_mono, out)
                return out

            # --- valid, fresh message
            self.stats.valid += 1
            self.last_message = msg
            self.last_valid_mono = now_mono
            if self.cfg.honor_advertised_timeout:
                self.timeout_s = min(self.cfg.heartbeat_timeout_ms, msg.heartbeat_timeout_ms) / 1000.0
            if gap:
                return out  # the gap message re-baselines sequencing but does not count toward recovery

            if not msg.monitor_ready:
                self._enter_fault(Fault.MONITOR_NOT_READY, msg.reason[:200] or "monitor not ready", now_mono, out)
                return out

            if self.in_fault:
                self.consecutive_good += 1
                if msg.state is RiskState.FLATTEN:
                    self._set_local(RiskState.FLATTEN, "published FLATTEN (operator instruction)", out)
                if self.consecutive_good >= self.cfg.recovery_snapshots_required:
                    prev_fault = self.fault
                    self.in_fault = False
                    self.fault = None
                    self.fault_detail = ""
                    self.stats.recoveries += 1
                    self.events.append((now_mono, f"RECOVERED after {prev_fault}: adopting {msg.state}"))
                    self._set_local(msg.state, f"recovered ({self.consecutive_good} valid snapshots): "
                                               f"{msg.reason[:200]}", out)
                return out

            self._set_local(msg.state, msg.reason[:300], out)
        return out

    def snapshot(self, now_mono: float) -> dict:
        with self._lock:
            m = self.last_message
            return {
                "local_state": str(self.effective_state(now_mono)),
                "in_fault": self.in_fault, "fault": self.fault.value if self.fault else None,
                "fault_detail": self.fault_detail, "publisher_id": self.publisher_id,
                "last_seq": self.last_seq, "consecutive_good": self.consecutive_good,
                "heartbeat_age_s": None if self.last_valid_mono is None else round(now_mono - self.last_valid_mono, 3),
                "timeout_s": self.timeout_s,
                "published_state": str(m.state) if m else None,
                "risk_score": m.risk_score if m else None,
                "data_origin": m.data_origin if m else None,
                "fallback_in_use": m.fallback_in_use if m else None,
                "stats": {"received": self.stats.received, "valid": self.stats.valid,
                          "recoveries": self.stats.recoveries, "faults": dict(self.stats.faults)},
            }
