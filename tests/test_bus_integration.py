"""End-to-end ZeroMQ tests: real PUB socket -> real SDK client over localhost TCP."""

import socket
import time

import pytest
import zmq

from vetosentinel.bus.publisher import Publisher, status_to_body
from vetosentinel.bus.schema import decode, encode
from vetosentinel.client import ClientConfig, StrategyHooks, VetoSentinelClient
from vetosentinel.config import BusConfig
from vetosentinel.engine.monitor import MonitorStatus
from vetosentinel.models import RiskState

pytestmark = pytest.mark.zmq


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeStatus:
    def __init__(self, state=RiskState.NORMAL):
        self.state = state
        self.ready = True

    def __call__(self):
        from datetime import UTC, datetime

        from vetosentinel.models import ComponentReading, RiskSnapshot
        snap = RiskSnapshot(as_of=datetime(2026, 1, 1, tzinfo=UTC),
                            components={"panic": ComponentReading(name="panic", available=True, z=0.5)},
                            risk_score=0.2, monitor_ready=self.ready, degraded=False)
        return MonitorStatus(run_id="t", mode="test", data_origin="synthetic_fixture", state=self.state,
                             snapshot=snap, decision=None, reason="unit test", top_headlines=[],
                             implementations={"panic": {"implementation": "x", "is_fallback": True}},
                             model_notes=[], adapters=[], counters={}, flatten=None)


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Recorder(StrategyHooks):
    def __init__(self):
        self.halts, self.cautions, self.flattens, self.changes = [], [], [], []

    def on_state_change(self, old, new, reason):
        self.changes.append((old, new))

    def on_halt(self, reason):
        self.halts.append(reason)

    def on_caution(self, f, reason):
        self.cautions.append(f)

    def on_flatten(self, reason):
        self.flattens.append(reason)


@pytest.fixture
def bus():
    port = free_port()
    cfg = BusConfig(pub_endpoint=f"tcp://127.0.0.1:{port}", heartbeat_interval_ms=100, heartbeat_timeout_ms=400)
    status = FakeStatus()
    live = {"t": time.monotonic()}
    pub = Publisher(cfg, status, lambda: live["t"], context=zmq.Context())
    pub.start()
    hooks = Recorder()
    client = VetoSentinelClient(f"tcp://127.0.0.1:{port}", config=ClientConfig(heartbeat_timeout_ms=400,
                               max_message_age_ms=1000), hooks=hooks, context=zmq.Context(), poll_ms=10)
    client.start()
    yield pub, client, status, hooks, live
    client.stop()
    pub.stop()


def test_wire_body_validates():
    body = status_to_body(FakeStatus()(), BusConfig())
    body.update(publisher_id="p", seq=1, kind="HEARTBEAT", ts="2026-01-01T00:00:00.000Z")
    msg = decode(encode(body))
    assert msg.state is RiskState.NORMAL and msg.fallback_in_use and msg.components.panic_z == 0.5


def test_client_starts_halted_then_follows_publisher(bus):
    pub, client, status, hooks, live = bus
    assert client.current_state is RiskState.HALT  # before any message (slow joiner / startup)
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    status.state = RiskState.CAUTION
    pub.notify("STATE")
    assert wait_for(lambda: client.current_state is RiskState.CAUTION)
    assert not client.can_submit_order(opening=True) and client.can_submit_order(opening=False)
    assert hooks.cautions


def test_publisher_death_triggers_watchdog_halt(bus):
    pub, client, status, hooks, live = bus
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    pub.stop()
    t0 = time.monotonic()
    assert wait_for(lambda: client.current_state is RiskState.HALT, timeout=2)
    assert time.monotonic() - t0 < 1.0  # timeout is 400 ms
    assert wait_for(lambda: bool(hooks.halts))
    assert client.status()["fault"] == "stale_heartbeat"


def test_pipeline_stall_forces_halt_in_heartbeats(bus):
    pub, client, status, hooks, live = bus
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    live["t"] = time.monotonic() - 3600  # analytics loop silent for an hour
    assert wait_for(lambda: client.current_state is RiskState.HALT)
    assert pub.last_sent["monitor_ready"] is False and "stalled" in pub.last_sent["reason"]
    live["t"] = time.monotonic() + 3600
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)


def test_injected_faults_fail_safe_and_recover(bus):
    pub, client, status, hooks, live = bus
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    for fault, expected in [("malformed", "invalid_message"), ("seq_gap", "sequence_gap")]:
        pub.inject(fault)
        assert wait_for(lambda e=expected: client.status()["stats"]["faults"][e] >= 1), fault
        assert wait_for(lambda: client.current_state is RiskState.NORMAL), fault
    pub.inject("silence_5s")
    assert wait_for(lambda: client.current_state is RiskState.HALT, timeout=2)
    assert wait_for(lambda: client.current_state is RiskState.NORMAL, timeout=8)


def test_publisher_restart_detected(bus):
    pub, client, status, hooks, live = bus
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    endpoint = pub.cfg.pub_endpoint
    pub.stop()
    time.sleep(0.2)
    pub2 = Publisher(pub.cfg, status, lambda: live["t"], context=zmq.Context())
    pub2.start()
    try:
        assert pub2.publisher_id != pub.publisher_id and not pub2.error, endpoint
        assert wait_for(lambda: client.status()["publisher_id"] == pub2.publisher_id, timeout=5)
        assert client.status()["stats"]["faults"]["publisher_restart"] == 1
        assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    finally:
        pub2.stop()


def test_monitor_not_ready_halts_client(bus):
    pub, client, status, hooks, live = bus
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    status.ready = False
    assert wait_for(lambda: client.current_state is RiskState.HALT)
    status.ready = True
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)


def test_hot_path_is_cheap(bus):
    pub, client, status, hooks, live = bus
    assert wait_for(lambda: client.current_state is RiskState.NORMAL)
    n = 20000
    t0 = time.perf_counter()
    for _ in range(n):
        client.can_submit_order()
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    assert per_call_us < 50  # local reads only; generous bound for slow CI machines
