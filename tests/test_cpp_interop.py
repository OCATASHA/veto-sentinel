"""Python publisher -> C++ client interop (skipped unless the C++ demo has been built)."""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
import zmq

from test_bus_integration import FakeStatus, free_port
from vetosentinel.bus.publisher import Publisher
from vetosentinel.bus.telemetry import FleetRegistry
from vetosentinel.config import BusConfig
from vetosentinel.models import RiskState

BIN = Path(__file__).resolve().parents[1] / "clients/cpp/build/strategy_demo"
pytestmark = [pytest.mark.zmq,
              pytest.mark.skipif(not BIN.exists(), reason="build clients/cpp first (see clients/cpp/README.md)")]


def test_cpp_client_follows_python_publisher_and_fails_safe():
    port = free_port()
    cfg = BusConfig(pub_endpoint=f"tcp://127.0.0.1:{port}", heartbeat_interval_ms=200, heartbeat_timeout_ms=1000)
    status = FakeStatus(RiskState.CAUTION)
    pub = Publisher(cfg, status, lambda: time.monotonic(), context=zmq.Context())
    pub.start()
    proc = subprocess.Popen([str(BIN), f"tcp://127.0.0.1:{port}", "6"], stdout=subprocess.PIPE, text=True)
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True)
    reader.start()
    try:
        time.sleep(2.0)
        pub.inject("malformed")
        time.sleep(0.8)
        pub.inject("seq_gap")
        time.sleep(1.2)
        status.state = RiskState.HALT
        pub.notify("STATE")
        proc.wait(timeout=20)
    finally:
        pub.stop()
        if proc.poll() is None:
            proc.kill()
    reader.join(5)
    out = "".join(lines)
    assert "startup state HALT" in out
    assert "LOCAL STATE HALT -> CAUTION" in out  # adopted published CAUTION after recovery
    summary = json.loads(next(ln for ln in lines if ln.startswith("SUMMARY ")).split(" ", 1)[1])
    assert summary["invalid"] >= 1 and summary["gaps"] >= 1 and summary["recoveries"] >= 2
    assert summary["published_state"] == "HALT"
    assert summary["check_mean_ns"] < 5000


def test_cpp_client_reports_to_the_fleet_and_says_goodbye():
    port, tport = free_port(), free_port()
    cfg = BusConfig(pub_endpoint=f"tcp://127.0.0.1:{port}", telemetry_endpoint=f"tcp://*:{tport}",
                    heartbeat_interval_ms=200, heartbeat_timeout_ms=1000)
    ctx = zmq.Context()
    reg = FleetRegistry(cfg, context=ctx)
    reg.start()
    pub = Publisher(cfg, FakeStatus(RiskState.NORMAL), lambda: time.monotonic(), context=ctx)
    pub.start()
    proc = subprocess.Popen([str(BIN), f"tcp://127.0.0.1:{port}", "0"], stdout=subprocess.DEVNULL,
                            env={"VETOSENTINEL_BOT_NAME": "interop bot", "VETOSENTINEL_SHADOW": "1"})
    try:
        end = time.monotonic() + 10
        bot = None
        while time.monotonic() < end and not (bot and bot["local_state"] == "NORMAL" and bot["check_ns"]):
            bot = next((b for b in reg.snapshot() if b["name"] == "interop bot"), None)
            time.sleep(0.1)
        assert bot is not None and bot["sdk"] == "cpp" and bot["local_state"] == "NORMAL" and bot["shadow"]
        assert 0 < bot["check_ns"] < 5000 and bot["pid"] == proc.pid and bot["bus_latency_ms"] is not None
        proc.terminate()  # SIGTERM: stops cleanly and says goodbye
        proc.wait(10)
        end = time.monotonic() + 3
        while time.monotonic() < end and reg.snapshot()[0]["status"] != "stopped":
            time.sleep(0.05)
        assert reg.snapshot()[0]["status"] == "stopped"
    finally:
        if proc.poll() is None:
            proc.kill()
        pub.stop()
        reg.stop()
