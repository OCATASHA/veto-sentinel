"""Fleet view: bots report their own local state back to the monitor (display only).

Each client SDK sends a small JSON report every second, and on every local state change, over a
ZeroMQ PUSH socket to the endpoint advertised in every bus message (``telemetry``). This thread
binds the PULL side and keeps the latest report per client. A bot that stops reporting for longer
than the heartbeat timeout is shown as silent; one that said goodbye is shown as stopped; both are
dropped after two minutes.

Reports are unauthenticated, like the bus itself, and only ever shown on the dashboard: nothing
here influences the risk state or any bot.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Literal

import zmq
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vetosentinel.config import BusConfig
from vetosentinel.models import RiskState

log = logging.getLogger(__name__)

MAX_REPORT_BYTES = 4096
MAX_BOTS = 50
FORGET_AFTER_S = 120.0


class BotReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    v: Literal[1]
    client_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)
    sdk: str = Field(default="", max_length=16)
    pid: int | None = None
    host: str | None = Field(default=None, max_length=64)
    local_state: RiskState
    published_state: RiskState | None = None
    in_fault: bool = False
    fault: str | None = Field(default=None, max_length=40)
    last_seq: int | None = None
    heartbeat_age_ms: float | None = Field(default=None, ge=0)
    received: int = Field(default=0, ge=0)
    valid: int = Field(default=0, ge=0)
    recoveries: int = Field(default=0, ge=0)
    shadow: bool = False
    would_block: int = Field(default=0, ge=0)
    check_ns: float | None = Field(default=None, ge=0, le=1e9)
    bus_latency_ms: float | None = Field(default=None, ge=-60_000, le=60_000)
    bye: bool = False


class FleetRegistry:
    def __init__(self, cfg: BusConfig, context: zmq.Context | None = None):
        self.cfg = cfg
        self.endpoint = cfg.telemetry_bind()
        self._ctx = context or zmq.Context.instance()
        self._lock = threading.Lock()
        self._bots: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rejected = 0
        self.error = ""

    def start(self) -> None:
        if not self.endpoint:
            return
        bound = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(bound,), name="vs-fleet", daemon=True)
        self._thread.start()
        bound.wait(5)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def accept(self, raw: bytes, now: float | None = None) -> bool:
        """Validate and store one report. Returns False (and counts it) if it is rejected."""
        try:
            if len(raw) > MAX_REPORT_BYTES:
                raise ValueError("report too large")
            rep = BotReport.model_validate(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, ValidationError):
            self.rejected += 1
            return False
        now = time.monotonic() if now is None else now
        with self._lock:
            known = self._bots.get(rep.client_id)
            if known is None and len(self._bots) >= MAX_BOTS:
                self.rejected += 1
                return False
            self._bots[rep.client_id] = {**rep.model_dump(mode="json"), "first_seen": known["first_seen"] if known else now,
                                         "last_seen": now}
        return True

    def snapshot(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.monotonic() if now is None else now
        silent_after = self.cfg.heartbeat_timeout_ms / 1000.0
        out = []
        with self._lock:
            for cid, b in list(self._bots.items()):
                age = now - b["last_seen"]
                if age > FORGET_AFTER_S:
                    del self._bots[cid]
                    continue
                status = "stopped" if b["bye"] else "silent" if age > silent_after else "ok"
                out.append({**{k: v for k, v in b.items() if k not in ("first_seen", "last_seen")},
                            "status": status, "report_age_s": round(age, 1),
                            "connected_s": round(b["last_seen"] - b["first_seen"], 1)})
        return sorted(out, key=lambda b: (b["sdk"] != "python", b["name"], b["client_id"]))

    def _run(self, bound: threading.Event) -> None:
        sock = self._ctx.socket(zmq.PULL)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVHWM, 1000)
        sock.setsockopt(zmq.MAXMSGSIZE, MAX_REPORT_BYTES)
        try:
            sock.bind(self.endpoint)
        except zmq.ZMQError as e:
            self.error = f"bind {self.endpoint} failed: {e}"
            log.warning("fleet view off: %s", self.error)
            bound.set()
            sock.close()
            return
        bound.set()
        log.info("fleet reports on %s", self.endpoint)
        try:
            while not self._stop.is_set():
                if sock.poll(200):
                    while True:
                        try:
                            self.accept(sock.recv(flags=zmq.NOBLOCK))
                        except zmq.Again:
                            break
        finally:
            sock.close()
