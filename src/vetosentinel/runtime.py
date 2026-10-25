"""Process wiring: controller thread + ZeroMQ publisher + optional in-process demo client + dashboard."""

from __future__ import annotations

import logging
import os
import random
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetosentinel.alerts import AlertDispatcher
from vetosentinel.assistant import Assistant
from vetosentinel.bus.publisher import Publisher
from vetosentinel.bus.telemetry import FleetRegistry
from vetosentinel.client import ClientConfig, VetoSentinelClient
from vetosentinel.config import Settings
from vetosentinel.store import Store

log = logging.getLogger(__name__)

CPP_BOT = Path(__file__).resolve().parents[2] / "clients" / "cpp" / "build" / "strategy_demo"
MAX_CPP_BOTS = 3


def connect_endpoint(bind_endpoint: str) -> str:
    """tcp://*:5556 -> tcp://127.0.0.1:5556 for local subscribers."""
    return bind_endpoint.replace("*", "127.0.0.1").replace("0.0.0.0", "127.0.0.1")


class DemoOrders:
    """The demo bot tries a simulated order every ``every_s`` through the real SDK gate
    (``can_submit_order``), so the dashboard can show what the gate decides. Nothing is sent
    anywhere: there is no broker. 60% of attempts open a position, 40% reduce one."""

    def __init__(self, client: VetoSentinelClient, every_s: float = 0.25, seed: int | None = None):
        self.client = client
        self.every_s = every_s
        self.rnd = random.Random(seed)
        self.lock = threading.Lock()
        self.recent: deque[tuple[int, bool, bool, bool]] = deque(maxlen=60)  # (id, opening, allowed, would_block)
        self.counts = {"attempts": 0, "allowed": 0, "blocked": 0, "would_block": 0}

    def attempt(self) -> tuple[bool, bool, bool]:
        opening = self.rnd.random() < 0.6
        truth = self.client.decide(opening)
        allowed = self.client.can_submit_order(opening)
        would_block = allowed and not truth  # shadow mode let it through
        with self.lock:
            self.counts["attempts"] += 1
            self.counts["allowed" if allowed else "blocked"] += 1
            self.counts["would_block"] += would_block
            self.recent.append((self.counts["attempts"], opening, allowed, would_block))
        return opening, allowed, would_block

    def loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.every_s):
            self.attempt()

    def to_json(self) -> dict[str, Any]:
        with self.lock:
            return {"counts": dict(self.counts), "recent": [list(x) for x in self.recent][-30:],
                    "every_s": self.every_s, "shadow": self.client.tracker.cfg.shadow}


@dataclass
class Runtime:
    settings: Settings
    store: Store
    controller: Any
    publisher: Publisher | None = None
    demo_client: VetoSentinelClient | None = None
    assistant: Assistant | None = None  # optional AI text for the dashboard (never feeds decisions)
    fleet: FleetRegistry | None = None
    orders: DemoOrders | None = None
    alerts: AlertDispatcher | None = None
    bots: dict[int, subprocess.Popen] = field(default_factory=dict)  # C++ demo bots started from the dashboard
    stop: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.assistant is None:
            a = self.assistant = Assistant(self.settings.ai)
            if a.enabled:
                log.info("AI assistant on (%s); advisory text only", ", ".join(self.settings.ai.models))

    def start(self, with_publisher: bool = True, with_demo_client: bool = True) -> None:
        if with_publisher:
            fleet = self.fleet = FleetRegistry(self.settings.bus)
            fleet.start()
            pub = self.publisher = Publisher(self.settings.bus, lambda: self.controller.monitor.status(),
                                             lambda: self.controller.liveness.t)

            def attach(monitor) -> None:
                monitor.listeners.append(pub.notify)
                pub.notify("STATE")

            attach(self.controller.monitor)
            self.controller.on_restart.append(attach)
            pub.start()
            if pub.error:
                raise RuntimeError(pub.error)
            if with_demo_client:
                self.demo_client = VetoSentinelClient(
                    connect_endpoint(self.settings.bus.pub_endpoint), self.settings.bus.topic,
                    config=ClientConfig(name="Example bot (Python, in the dashboard)"))
                self.demo_client.start()
                self.orders = DemoOrders(self.demo_client)
                self._thread(self.orders.loop, "vs-demo-orders")
        alerts = self.alerts = AlertDispatcher(self.settings.alerts, lambda: self.controller.monitor.status(),
                                               "replay" if hasattr(self.controller, "restart") else "live")
        self.controller.monitor.listeners.append(alerts.notify)
        self.controller.on_restart.append(lambda m: m.listeners.append(alerts.notify))
        alerts.start()
        self._thread(self.controller.loop, "vs-analytics")

    def _thread(self, target, name: str) -> None:
        t = threading.Thread(target=target, args=(self.stop,), name=name, daemon=True)
        t.start()
        self.threads.append(t)

    # ------------------------------------------------------------------ C++ demo bots (dashboard buttons)
    def spawn_cpp_bot(self) -> dict[str, Any]:
        """Start the example C++ strategy (clients/cpp) as a separate process. Demo only."""
        if not self.settings.bus.allow_fault_injection:
            raise PermissionError("demo controls are turned off (bus.allow_fault_injection = false)")
        if not CPP_BOT.exists():
            raise FileNotFoundError("the C++ client isn't built yet: cmake -S clients/cpp -B clients/cpp/build "
                                    "&& cmake --build clients/cpp/build")
        alive = [p for p in self.bots.values() if p.poll() is None]
        if len(alive) >= MAX_CPP_BOTS:
            raise RuntimeError(f"{MAX_CPP_BOTS} C++ bots are already running")
        name = f"C++ bot #{len(self.bots) + 1}"
        p = subprocess.Popen([str(CPP_BOT), connect_endpoint(self.settings.bus.pub_endpoint), "0"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env={**os.environ, "VETOSENTINEL_BOT_NAME": name})
        self.bots[p.pid] = p
        log.info("started %s (pid %d)", name, p.pid)
        return {"pid": p.pid, "name": name}

    def stop_cpp_bot(self, pid: int, hard: bool) -> dict[str, Any]:
        """Stop a bot started here: politely (SIGTERM: it says goodbye) or by pulling the plug
        (SIGKILL: it just goes silent, as if it crashed)."""
        p = self.bots.get(pid)
        if p is None:
            raise KeyError(pid)
        if p.poll() is None:
            p.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
        return {"pid": pid, "how": "kill" if hard else "stop"}

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(5)
        for p in self.bots.values():
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 3
        for p in self.bots.values():
            try:
                p.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
        if self.demo_client:
            self.demo_client.stop()
        if self.publisher:
            self.publisher.stop()
        if self.fleet:
            self.fleet.stop()
        if self.alerts:
            self.alerts.stop()
        self.store.end_run(self.controller.monitor.run_id)
