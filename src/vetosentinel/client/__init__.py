"""Python client SDK.

    client = VetoSentinelClient("tcp://127.0.0.1:5556", hooks=MyHooks())
    client.start()
    ...
    if client.can_submit_order(opening=True):   # local read only, never touches the network
        ...

``can_submit_order`` and ``current_state`` read local fields only. A background thread
receives and validates messages and runs the watchdog. As a second line of defence, the hot
path itself checks heartbeat age against the monotonic clock, so a stalled background thread
still yields HALT.

Shadow mode (``ClientConfig(shadow=True)``): the gate always allows, counts what it would have
blocked (``would_block``) and does not call the action hooks, so Veto Sentinel can run next to
a strategy before anyone relies on it. ``decide()`` always gives the real answer.

Fleet reports: when the monitor advertises a telemetry endpoint, the background thread sends a
small report about this client every second (and on every local state change) over a separate
ZeroMQ PUSH socket, never from the order path. Turn off with ``ClientConfig(report=False)``.

There is NO broker integration. Hooks are where a strategy wires its own (simulated or
real, human-reviewed) handling: e.g. ``on_halt`` should request cancellation of resting
orders; ``on_flatten`` should page a human. Veto Sentinel never sends orders itself.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

import zmq

from vetosentinel.client.tracker import ClientConfig, Fault, StateTracker, Transition
from vetosentinel.models import RiskState
from vetosentinel.timeutil import iso_ms, utcnow

__all__ = ["ClientConfig", "Fault", "VetoSentinelClient", "StateTracker", "StrategyHooks", "Transition",
           "resolve_telemetry"]

log = logging.getLogger(__name__)

REPORT_EVERY_S = 1.0
BENCH_EVERY_S = 10.0


def resolve_telemetry(advertised: str | None, sub_endpoint: str) -> str | None:
    """The monitor advertises its bind address; a wildcard or loopback host there means "the
    host I subscribed to"."""
    if not advertised:
        return None
    try:
        adv, sub = urlsplit(advertised), urlsplit(sub_endpoint)
    except ValueError:
        return None
    if adv.scheme != "tcp" or adv.port is None:
        return None
    host = adv.hostname or "*"
    if host in ("*", "0.0.0.0", "127.0.0.1", "localhost") and sub.hostname:
        host = sub.hostname
    if host == "*":
        host = "127.0.0.1"
    return f"tcp://{host}:{adv.port}"


class StrategyHooks:
    """Override any of these. They run on the client's background thread; keep them short."""

    def on_state_change(self, old: RiskState, new: RiskState, reason: str) -> None:
        pass

    def on_caution(self, position_fraction: float, reason: str) -> None:
        """Apply a reduced position limit and stop opening new positions."""

    def on_halt(self, reason: str) -> None:
        """Request cancellation of resting orders (strategy-owned; no broker integration here)."""

    def on_flatten(self, reason: str) -> None:
        """Operator FLATTEN instruction: notify a human-controlled process. Do NOT auto-liquidate."""


class VetoSentinelClient:
    def __init__(self, endpoint: str = "tcp://127.0.0.1:5556", topic: str = "vs.v1",
                 config: ClientConfig | None = None, hooks: StrategyHooks | None = None,
                 context: zmq.Context | None = None, poll_ms: int = 50):
        self.endpoint = endpoint
        self.topic = topic.encode()
        self.tracker = StateTracker(config)
        self.hooks = hooks or StrategyHooks()
        self.client_id = uuid.uuid4().hex[:12]
        self.would_block = 0  # shadow mode: orders it would have blocked (approximate under threads)
        self.check_ns: float | None = None  # measured cost of one can_submit_order() call
        self._ctx = context or zmq.Context.instance()
        self._poll_ms = poll_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._report_seq = 0

    # ------------------------------------------------------------------ hot path (local only)
    @property
    def current_state(self) -> RiskState:
        return self.tracker.effective_state(time.monotonic())

    def decide(self, opening: bool = True) -> bool:
        """The real answer, ignoring shadow mode. NORMAL: any order. CAUTION: only
        position-reducing orders. HALT/FLATTEN: nothing."""
        st = self.tracker.effective_state(time.monotonic())
        if st is RiskState.NORMAL:
            return True
        if st is RiskState.CAUTION:
            return not opening
        return False

    def can_submit_order(self, opening: bool = True) -> bool:
        """The order gate. In shadow mode it always allows and counts what it would have blocked."""
        ok = self.decide(opening)
        if not ok and self.tracker.cfg.shadow:
            self.would_block += 1
            return True
        return ok

    def position_limit(self, normal_limit: float) -> float:
        st = self.current_state
        if st is RiskState.NORMAL or self.tracker.cfg.shadow:
            return normal_limit
        if st is RiskState.CAUTION:
            return normal_limit * self.tracker.cfg.caution_position_fraction
        return 0.0

    def status(self) -> dict:
        return {**self.tracker.snapshot(time.monotonic()), "client_id": self.client_id,
                "would_block": self.would_block, "check_ns": self.check_ns}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vs-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def _dispatch(self, transitions: list[Transition]) -> None:
        shadow = self.tracker.cfg.shadow
        for t in transitions:
            log.info("Veto Sentinel local state %s -> %s: %s%s", t.old, t.new, t.reason,
                     " (SHADOW MODE: not enforced)" if shadow else "")
            try:
                self.hooks.on_state_change(t.old, t.new, t.reason)
                if t.new is RiskState.CAUTION and not shadow:
                    self.hooks.on_caution(self.tracker.cfg.caution_position_fraction, t.reason)
                elif t.new is RiskState.HALT and not shadow:
                    self.hooks.on_halt(t.reason)
                elif t.new is RiskState.FLATTEN:
                    self.hooks.on_flatten(t.reason)  # always: it tells a human, it changes no orders
            except Exception:  # noqa: BLE001 - a hook bug must not kill the watchdog
                log.exception("strategy hook raised")

    # ------------------------------------------------------------------ fleet reports (background only)
    def measure_check(self, n: int = 20_000) -> float:
        """Average cost of one can_submit_order() call, timed over n calls (ns)."""
        would = self.would_block
        f = self.can_submit_order
        t0 = time.perf_counter_ns()
        for _ in range(n):
            f(True)
        self.would_block = would  # benchmark calls are not orders
        self.check_ns = (time.perf_counter_ns() - t0) / n
        return self.check_ns

    def report(self, bye: bool = False) -> dict:
        snap = self.tracker.snapshot(time.monotonic())
        self._report_seq += 1
        age = snap["heartbeat_age_s"]
        cfg = self.tracker.cfg
        return {
            "v": 1, "kind": "BOT", "client_id": self.client_id, "seq": self._report_seq, "sent_ts": iso_ms(utcnow()),
            "name": cfg.name or f"Python bot on {socket.gethostname()}", "sdk": "python", "pid": os.getpid(),
            "host": socket.gethostname()[:64], "local_state": snap["local_state"],
            "published_state": snap["published_state"], "in_fault": snap["in_fault"], "fault": snap["fault"],
            "last_seq": snap["last_seq"], "heartbeat_age_ms": None if age is None else round(age * 1000, 1),
            "received": snap["stats"]["received"], "valid": snap["stats"]["valid"],
            "recoveries": snap["stats"]["recoveries"], "shadow": cfg.shadow, "would_block": self.would_block,
            "check_ns": self.check_ns, "bus_latency_ms": snap["bus_latency_ms"], "bye": bye,
        }

    def _send_report(self, push: zmq.Socket | None, bye: bool = False) -> None:
        if push is None:
            return
        try:
            push.send(json.dumps(self.report(bye), separators=(",", ":")).encode(), flags=zmq.NOBLOCK)
        except zmq.Again:
            pass  # nobody listening right now: reports are best effort

    def _run(self) -> None:
        sock = self._ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVHWM, 1000)
        sock.setsockopt(zmq.RECONNECT_IVL, 200)
        sock.setsockopt(zmq.RECONNECT_IVL_MAX, 2000)
        sock.setsockopt(zmq.SUBSCRIBE, self.topic)
        sock.connect(self.endpoint)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        push: zmq.Socket | None = None
        push_to: str | None = None
        next_report = next_bench = time.monotonic()
        try:
            while not self._stop.is_set():
                events = dict(poller.poll(self._poll_ms))
                changed: list[Transition] = []
                if sock in events:
                    while True:
                        try:
                            frames = sock.recv_multipart(flags=zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        if len(frames) != 2 or frames[0] != self.topic:
                            payload = b"<bad framing>"
                        else:
                            payload = frames[1]
                        changed += self.tracker.on_payload(payload, time.monotonic(), utcnow())
                changed += self.tracker.check_watchdog(time.monotonic())
                self._dispatch(changed)
                # fleet report: (re)connect to the advertised endpoint, then report every second
                target = (resolve_telemetry(self.tracker.telemetry_endpoint, self.endpoint)
                          if self.tracker.cfg.report else None)
                if target != push_to:
                    if push is not None:
                        push.close()
                    push, push_to = None, target
                    if target:
                        p = self._ctx.socket(zmq.PUSH)
                        p.setsockopt(zmq.LINGER, 200)
                        p.setsockopt(zmq.SNDHWM, 10)
                        p.connect(target)
                        push = p
                now = time.monotonic()
                if push is not None and now >= next_bench:
                    self.measure_check()
                    next_bench = now + BENCH_EVERY_S
                if push is not None and (changed or now >= next_report):
                    self._send_report(push)
                    next_report = now + REPORT_EVERY_S
            self._send_report(push, bye=True)
        except Exception:  # noqa: BLE001
            log.exception("client subscriber thread crashed; hot path will report HALT once stale")
        finally:
            sock.close()
            if push is not None:
                push.close()
