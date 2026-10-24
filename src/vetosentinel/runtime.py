"""Process wiring: controller thread + ZeroMQ publisher + optional in-process demo client + dashboard."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from vetosentinel.bus.publisher import Publisher
from vetosentinel.client import VetoSentinelClient
from vetosentinel.config import Settings
from vetosentinel.store import Store

log = logging.getLogger(__name__)


def connect_endpoint(bind_endpoint: str) -> str:
    """tcp://*:5556 -> tcp://127.0.0.1:5556 for local subscribers."""
    return bind_endpoint.replace("*", "127.0.0.1").replace("0.0.0.0", "127.0.0.1")


@dataclass
class Runtime:
    settings: Settings
    store: Store
    controller: Any
    publisher: Publisher | None = None
    demo_client: VetoSentinelClient | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)

    def start(self, with_publisher: bool = True, with_demo_client: bool = True) -> None:
        if with_publisher:
            self.publisher = Publisher(self.settings.bus, lambda: self.controller.monitor.status(),
                                       lambda: self.controller.liveness.t)

            def attach(monitor) -> None:
                monitor.listeners.append(self.publisher.notify)
                self.publisher.notify("STATE")

            attach(self.controller.monitor)
            self.controller.on_restart.append(attach)
            self.publisher.start()
            if self.publisher.error:
                raise RuntimeError(self.publisher.error)
            if with_demo_client:
                self.demo_client = VetoSentinelClient(connect_endpoint(self.settings.bus.pub_endpoint),
                                                     self.settings.bus.topic)
                self.demo_client.start()
        t = threading.Thread(target=self.controller.loop, args=(self.stop,), name="vs-analytics", daemon=True)
        t.start()
        self.threads.append(t)

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(5)
        if self.demo_client:
            self.demo_client.stop()
        if self.publisher:
            self.publisher.stop()
        self.store.end_run(self.controller.monitor.run_id)
