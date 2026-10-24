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

There is NO broker integration. Hooks are where a strategy wires its own (simulated or
real, human-reviewed) handling: e.g. ``on_halt`` should request cancellation of resting
orders; ``on_flatten`` should page a human. Veto Sentinel never sends orders itself.
"""

from __future__ import annotations

import logging
import threading
import time

import zmq

from vetosentinel.client.tracker import ClientConfig, Fault, StateTracker, Transition
from vetosentinel.models import RiskState
from vetosentinel.timeutil import utcnow

__all__ = ["ClientConfig", "Fault", "VetoSentinelClient", "StateTracker", "StrategyHooks", "Transition"]

log = logging.getLogger(__name__)


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
        self._ctx = context or zmq.Context.instance()
        self._poll_ms = poll_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ hot path (local only)
    @property
    def current_state(self) -> RiskState:
        return self.tracker.effective_state(time.monotonic())

    def can_submit_order(self, opening: bool = True) -> bool:
        """NORMAL: any order. CAUTION: only position-reducing orders. HALT/FLATTEN: nothing."""
        st = self.tracker.effective_state(time.monotonic())
        if st is RiskState.NORMAL:
            return True
        if st is RiskState.CAUTION:
            return not opening
        return False

    def position_limit(self, normal_limit: float) -> float:
        st = self.current_state
        if st is RiskState.NORMAL:
            return normal_limit
        if st is RiskState.CAUTION:
            return normal_limit * self.tracker.cfg.caution_position_fraction
        return 0.0

    def status(self) -> dict:
        return self.tracker.snapshot(time.monotonic())

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vs-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def _dispatch(self, transitions: list[Transition]) -> None:
        for t in transitions:
            log.info("Veto Sentinel local state %s -> %s: %s", t.old, t.new, t.reason)
            try:
                self.hooks.on_state_change(t.old, t.new, t.reason)
                if t.new is RiskState.CAUTION:
                    self.hooks.on_caution(self.tracker.cfg.caution_position_fraction, t.reason)
                elif t.new is RiskState.HALT:
                    self.hooks.on_halt(t.reason)
                elif t.new is RiskState.FLATTEN:
                    self.hooks.on_flatten(t.reason)
            except Exception:  # noqa: BLE001 - a hook bug must not kill the watchdog
                log.exception("strategy hook raised")

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
        try:
            while not self._stop.is_set():
                events = dict(poller.poll(self._poll_ms))
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
                        self._dispatch(self.tracker.on_payload(payload, time.monotonic(), utcnow()))
                self._dispatch(self.tracker.check_watchdog(time.monotonic()))
        except Exception:  # noqa: BLE001
            log.exception("client subscriber thread crashed; hot path will report HALT once stale")
        finally:
            sock.close()
