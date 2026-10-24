"""ZeroMQ PUB publisher with heartbeat.

One thread owns the PUB socket (ZeroMQ sockets are not thread-safe). It sends:
  * a HEARTBEAT with a full snapshot every ``heartbeat_interval_ms``;
  * an immediate STATE or CONTROL message when ``notify(kind)`` is called.
``seq`` is assigned in that thread at send time, so it is strictly monotonic.

Self-watchdog: if the analytics loop has not reported liveness within
``pipeline_stale_ms``, outgoing messages are overridden to ``state=HALT,
monitor_ready=false`` with the reason, so a hung pipeline cannot keep advertising a stale
NORMAL. If the publisher process dies, clients stop receiving heartbeats and their own
watchdogs HALT.

NOTE: ZeroMQ PUB/SUB does not guarantee delivery (slow joiners, HWM drops, disconnects).
The heartbeat + client watchdog make loss *fail safe*; they do not make the transport reliable.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import zmq

from vetosentinel.bus.schema import SCHEMA_VERSION, Kind, encode
from vetosentinel.config import BusConfig
from vetosentinel.engine.monitor import MonitorStatus
from vetosentinel.models import RiskState
from vetosentinel.timeutil import iso_ms, utcnow

log = logging.getLogger(__name__)


def status_to_body(st: MonitorStatus, cfg: BusConfig) -> dict[str, Any]:
    snap = st.snapshot
    comps = {"panic_z": None, "drift_z": None, "volume_z": None}
    monitor_ready = False
    degraded, degraded_reasons = True, ["no assessment yet"]
    risk = None
    as_of = None
    if snap is not None:
        for k in ("panic", "drift", "volume"):
            c = snap.components.get(k)
            comps[f"{k}_z"] = c.z if c is not None and c.available else None
        monitor_ready = snap.monitor_ready
        degraded, degraded_reasons = snap.degraded, list(snap.degraded_reasons)
        if not snap.monitor_ready:
            degraded_reasons = list(snap.not_ready_reasons) + degraded_reasons
        risk = snap.risk_score
        as_of = iso_ms(snap.as_of)
    impls = {k: v["implementation"] for k, v in st.implementations.items()}
    return {
        "schema_version": SCHEMA_VERSION, "state": str(st.state), "as_of": as_of, "risk_score": risk,
        "components": comps, "reason": st.reason[:2000], "degraded": degraded,
        "degraded_reasons": [r[:300] for r in degraded_reasons[:20]],
        "top_headlines": st.top_headlines[: cfg.top_headlines], "monitor_ready": monitor_ready,
        "heartbeat_interval_ms": cfg.heartbeat_interval_ms, "heartbeat_timeout_ms": cfg.heartbeat_timeout_ms,
        "data_origin": st.data_origin, "implementations": impls,
        "fallback_in_use": any(v["is_fallback"] for v in st.implementations.values()),
        "control": st.control,
        "focus": st.focus.get("text") or None,
    }


class Publisher:
    def __init__(self, cfg: BusConfig, get_status: Callable[[], MonitorStatus],
                 get_liveness: Callable[[], float], context: zmq.Context | None = None):
        self.cfg = cfg
        self.get_status = get_status
        self.get_liveness = get_liveness  # time.monotonic() of last analytics-loop iteration
        self.publisher_id = uuid.uuid4().hex[:16]
        self.seq = 0
        self._ctx = context or zmq.Context.instance()
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = {"STATE": 0, "HEARTBEAT": 0, "CONTROL": 0}
        self.last_sent: dict[str, Any] | None = None
        self.bound = threading.Event()
        self.error: str = ""
        self._silence_until = 0.0
        self.injected: list[str] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vs-publisher", daemon=True)
        self._thread.start()
        self.bound.wait(5)

    def stop(self) -> None:
        self._stop.set()
        self._q.put("STOP")
        if self._thread:
            self._thread.join(5)

    def notify(self, kind: str) -> None:
        self._q.put(kind)

    # ------------------------------------------------------------------ demo fault injection
    FAULTS = ("silence_5s", "malformed", "seq_gap")

    def inject(self, fault: str) -> None:
        """DEMO ONLY: exercise client fail-safes. Disabled unless ``allow_fault_injection``."""
        if not self.cfg.allow_fault_injection:
            raise PermissionError("fault injection disabled by configuration")
        if fault not in self.FAULTS:
            raise ValueError(f"unknown fault {fault!r}; expected one of {self.FAULTS}")
        self.injected.append(fault)
        log.warning("DEMO FAULT INJECTION: %s", fault)
        self._q.put("INJECT:" + fault)

    def build(self, kind: str) -> dict[str, Any]:
        body = status_to_body(self.get_status(), self.cfg)
        stale_s = time.monotonic() - self.get_liveness()
        if stale_s * 1000 > self.cfg.pipeline_stale_ms:
            body.update(state=str(RiskState.HALT), monitor_ready=False,
                        reason=f"analytics pipeline stalled ({stale_s:.1f}s without liveness); fail-safe HALT")
            body["degraded_reasons"] = ["pipeline stalled"] + body["degraded_reasons"][:19]
        body.update(publisher_id=self.publisher_id, seq=self.seq + 1, kind=kind, ts=iso_ms(utcnow()))
        return body

    def _run(self) -> None:
        sock = self._ctx.socket(zmq.PUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SNDHWM, 1000)
        try:
            sock.bind(self.cfg.pub_endpoint)
        except zmq.ZMQError as e:
            self.error = f"bind {self.cfg.pub_endpoint} failed: {e}"
            log.error(self.error)
            self.bound.set()
            sock.close()
            return
        self.bound.set()
        log.info("publishing on %s topic=%s publisher_id=%s", self.cfg.pub_endpoint, self.cfg.topic,
                 self.publisher_id)
        topic = self.cfg.topic.encode()
        interval = self.cfg.heartbeat_interval_ms / 1000.0
        next_hb = time.monotonic()
        try:
            while not self._stop.is_set():
                timeout = max(0.0, next_hb - time.monotonic())
                try:
                    kind = self._q.get(timeout=timeout)
                except queue.Empty:
                    kind = "HEARTBEAT"
                if kind == "STOP":
                    break
                if kind.startswith("INJECT:"):
                    self._do_inject(sock, topic, kind.split(":", 1)[1])
                    continue
                if time.monotonic() < self._silence_until:
                    if kind == "HEARTBEAT":
                        next_hb = time.monotonic() + interval
                    continue
                if kind not in (Kind.STATE, Kind.CONTROL, Kind.HEARTBEAT):
                    continue
                try:
                    body = self.build(kind)
                    payload = encode(body)
                    self.seq = body["seq"]  # commit only once the message is encodable
                    sock.send_multipart([topic, payload], flags=zmq.NOBLOCK)
                    self.sent[kind] += 1
                    self.last_sent = body
                except zmq.Again:
                    log.warning("PUB send would block; message dropped (seq=%d)", self.seq)
                except Exception:  # noqa: BLE001 - never let the publisher thread die silently
                    log.exception("failed to build/send message")
                if kind == "HEARTBEAT":
                    next_hb = time.monotonic() + interval
        finally:
            sock.close()

    def _do_inject(self, sock: zmq.Socket, topic: bytes, fault: str) -> None:
        if fault == "silence_5s":
            self._silence_until = time.monotonic() + 5.0
        elif fault == "malformed":
            sock.send_multipart([topic, b'{"schema_version": 1, "state": "NORMAL", "injected": "malformed"}'])
        elif fault == "seq_gap":
            self.seq += 3  # skip three sequence numbers; clients must detect the gap
