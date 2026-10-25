"""Shadow mode, fleet reports (reverse channel), the demo order gate, webhook alerts, preflight."""

import hashlib
import hmac
import json
import time

import httpx
import pytest
import zmq

from helpers import Feed
from test_bus_integration import FakeStatus, free_port, wait_for
from vetosentinel.alerts import AlertDispatcher, Target, event_from_status, sign, text_of
from vetosentinel.bus.publisher import Publisher
from vetosentinel.bus.telemetry import FleetRegistry
from vetosentinel.client import ClientConfig, StrategyHooks, VetoSentinelClient, resolve_telemetry
from vetosentinel.config import AlertsConfig, BusConfig, WebhookConfig
from vetosentinel.models import RiskState
from vetosentinel.runtime import DemoOrders


def test_telemetry_endpoint_resolution():
    sub = "tcp://10.0.0.5:5556"
    assert resolve_telemetry("tcp://*:5656", sub) == "tcp://10.0.0.5:5656"  # wildcard: the host we subscribe to
    assert resolve_telemetry("tcp://127.0.0.1:5656", sub) == "tcp://10.0.0.5:5656"
    assert resolve_telemetry("tcp://monitor.example:5656", sub) == "tcp://monitor.example:5656"
    assert resolve_telemetry("tcp://*:5656", "tcp://*:5556") == "tcp://127.0.0.1:5656"
    assert resolve_telemetry(None, sub) is None and resolve_telemetry("ipc://x", sub) is None
    assert BusConfig(pub_endpoint="tcp://*:5557").telemetry_bind() == "tcp://*:5657"
    assert BusConfig(telemetry_endpoint="").telemetry_bind() is None


class Hooks(StrategyHooks):
    def __init__(self):
        self.calls = []

    def on_halt(self, reason):
        self.calls.append("halt")

    def on_caution(self, position_fraction, reason):
        self.calls.append("caution")

    def on_flatten(self, reason):
        self.calls.append("flatten")


def _client(state: str, shadow: bool, hooks=None) -> VetoSentinelClient:
    c = VetoSentinelClient(config=ClientConfig(shadow=shadow), hooks=hooks)  # never started: no sockets
    feed = Feed(c.tracker)
    feed.mono = time.monotonic()
    for _ in range(3):
        feed.send(state=state)
    return c


def test_shadow_mode_never_blocks_but_counts_and_keeps_hands_off():
    enforced, shadow = _client("HALT", False), _client("HALT", True)
    assert not enforced.can_submit_order(True) and not enforced.can_submit_order(False)
    assert shadow.can_submit_order(True) and shadow.can_submit_order(False)
    assert not shadow.decide(True) and shadow.would_block == 2
    assert shadow.position_limit(100.0) == 100.0 and enforced.position_limit(100.0) == 0.0
    hooks = Hooks()
    c = _client("NORMAL", True, hooks)
    from vetosentinel.client import Transition
    c._dispatch([Transition(RiskState.NORMAL, RiskState.CAUTION, "x"), Transition(RiskState.CAUTION, RiskState.HALT, "x"),
                 Transition(RiskState.HALT, RiskState.FLATTEN, "x")])
    assert hooks.calls == ["flatten"]  # no limit changes or cancellations in shadow mode; humans still told
    assert c.status()["shadow"] and "would_block" in c.status()


def test_demo_orders_go_through_the_real_gate():
    for state, opening_ok, reduce_ok in (("NORMAL", True, True), ("CAUTION", False, True), ("HALT", False, False)):
        orders = DemoOrders(_client(state, False), seed=1)
        for _ in range(40):
            opening, allowed, would = orders.attempt()
            assert allowed == (opening_ok if opening else reduce_ok) and not would
    shadow = DemoOrders(_client("HALT", True), seed=1)
    for _ in range(20):
        shadow.attempt()
    j = shadow.to_json()
    assert j["shadow"] and j["counts"]["blocked"] == 0 and j["counts"]["would_block"] == 20
    assert len(j["recent"]) == 20 and j["recent"][-1][0] == 20


def _report(**kw):
    base = {"v": 1, "client_id": "abc123", "name": "bot", "sdk": "cpp", "pid": 42, "local_state": "NORMAL",
            "published_state": "NORMAL", "check_ns": 25.0, "bye": False}
    return json.dumps({**base, **kw}).encode()


def test_fleet_registry_validates_and_ages_reports():
    reg = FleetRegistry(BusConfig(heartbeat_timeout_ms=3000))
    assert reg.accept(_report(), now=100.0)
    assert [b["status"] for b in reg.snapshot(now=101.0)] == ["ok"]
    assert reg.snapshot(now=104.5)[0]["status"] == "silent"  # nothing for longer than the heartbeat timeout
    assert reg.accept(_report(bye=True), now=105.0) and reg.snapshot(now=105.1)[0]["status"] == "stopped"
    assert reg.snapshot(now=300.0) == []  # forgotten
    for bad in (b"not json", _report(local_state="MAYBE"), _report(v=2), _report(check_ns=-1), b"{" * 5000):
        assert not reg.accept(bad, now=1.0)
    assert reg.rejected == 5


def test_python_client_reports_to_the_fleet_and_says_goodbye():
    port, tport = free_port(), free_port()
    cfg = BusConfig(pub_endpoint=f"tcp://127.0.0.1:{port}", telemetry_endpoint=f"tcp://127.0.0.1:{tport}",
                    heartbeat_interval_ms=100, heartbeat_timeout_ms=400)
    ctx = zmq.Context()
    reg = FleetRegistry(cfg, context=ctx)
    reg.start()
    pub = Publisher(cfg, FakeStatus(), lambda: time.monotonic(), context=ctx)
    pub.start()
    client = VetoSentinelClient(f"tcp://127.0.0.1:{port}", config=ClientConfig(heartbeat_timeout_ms=400, name="unit bot"),
                                context=ctx, poll_ms=10)
    client.start()
    try:
        assert wait_for(lambda: any(b["name"] == "unit bot" and b["local_state"] == "NORMAL" for b in reg.snapshot()))
        bot = next(b for b in reg.snapshot() if b["name"] == "unit bot")
        assert bot["sdk"] == "python" and bot["check_ns"] and bot["check_ns"] > 0 and bot["bus_latency_ms"] is not None
        client.stop()
        assert wait_for(lambda: reg.snapshot()[0]["status"] == "stopped", 3)
    finally:
        client.stop()
        pub.stop()
        reg.stop()


# ---------------------------------------------------------------------------- alerts
def _event(state="HALT", prev="CAUTION"):
    ev = event_from_status("STATE", FakeStatus(RiskState[state])(), prev, "replay")
    ev["components"] = {"panic_z": 2.6, "drift_z": 2.7, "volume_z": 0.4}
    return ev


def test_alert_formats_and_signature(monkeypatch):
    monkeypatch.setenv("T_URL", "https://hooks.example/abc")
    monkeypatch.setenv("T_SECRET", "s3cret")
    title, text = text_of(_event(), 2, "https://dash.example")
    assert title == "Veto Sentinel: Stop" and "was Be careful" in text and "negativity z 2.6" in text
    assert "+2 earlier changes" in text and "never trades" in text and "simulated clock" in text
    ntfy = Target(WebhookConfig(kind="ntfy", url_env="T_URL"))
    h, body = ntfy.request(_event(), 0, "https://dash.example")
    assert h["Priority"] == "5" and h["Title"] == title and h["Click"] == "https://dash.example" and b"Risk score" in body
    h, body = Target(WebhookConfig(kind="slack", url_env="T_URL")).request(_event(), 0, "")
    assert json.loads(body)["text"].startswith("*Veto Sentinel: Stop*")
    js = Target(WebhookConfig(kind="json", url_env="T_URL", secret_env="T_SECRET"))
    h, body = js.request(_event(), 0, "")
    payload = json.loads(body)
    assert payload["state"] == "HALT" and payload["automatic_execution"] is False
    expected = hmac.new(b"s3cret", h["X-VetoSentinel-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert h["X-VetoSentinel-Signature"] == "sha256=" + expected == sign("s3cret", h["X-VetoSentinel-Timestamp"], body)
    assert js.info()["host"] == "hooks.example" and "abc" not in json.dumps(js.info())  # never the full URL


def test_alerts_are_rate_limited_folded_and_retried(monkeypatch):
    monkeypatch.setenv("T_URL", "https://hooks.example/abc")
    sent, codes = [], []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(codes.pop(0) if codes else 200)

    status = FakeStatus(RiskState.NORMAL)
    clock = {"t": 0.0}
    d = AlertDispatcher(AlertsConfig(min_interval_seconds=10, webhooks=[WebhookConfig(kind="ntfy", url_env="T_URL"),
                                                                        WebhookConfig(kind="slack", url_env="UNSET_VAR")]),
                        status, "replay", client=httpx.Client(transport=httpx.MockTransport(handler)),
                        clock=lambda: clock["t"])
    d.retry_delays = (0.0, 0.0)
    assert len(d.active) == 1  # a target without a URL is skipped
    for st in ("CAUTION", "HALT", "CAUTION"):
        status.state = RiskState[st]
        d.notify("STATE")
    d.notify("STATE")  # same level again: nothing new
    d.pump()
    assert len(sent) == 1 and "Be careful" in sent[0] and "+2 earlier changes" in sent[0]  # folded into one
    status.state = RiskState.NORMAL
    d.notify("STATE")
    d.pump()
    assert len(sent) == 1  # within 10 s of the last alert: waits
    clock["t"] = 11
    codes.extend([500, 503])
    d.pump()
    assert len(sent) == 4 and "All clear" in sent[-1]  # two failures retried, third attempt delivered
    assert d.info()["targets"][0]["sent"] == 2 and d.info()["targets"][0]["failed"] == 0


def test_preflight_runs_offline(capsys):
    from vetosentinel.preflight import run

    code = run(online=False)
    out = capsys.readouterr().out
    assert code in (0, 1) and "Veto Sentinel preflight" in out and "Demo data" in out and "Ports for run_live.sh" in out


@pytest.fixture(scope="module")
def app():
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from vetosentinel.config import Settings
    from vetosentinel.dashboard.app import create_app
    from vetosentinel.dashboard.auth import Auth, Credentials
    from vetosentinel.runtime import Runtime
    from vetosentinel.service import Backends, ReplayController
    from vetosentinel.store import Store

    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    store = Store(":memory:")
    ctrl = ReplayController(s, "fixtures/synthetic_demo.jsonl", store, Backends(s))
    rt = Runtime(s, store, ctrl)
    ctrl.seek(datetime(2026, 2, 5, 6, tzinfo=UTC))
    client = TestClient(create_app(rt, auth=Auth(Credentials.from_password("tester", "pw-for-tests"))))
    assert client.post("/api/login", json={"username": "tester", "password": "pw-for-tests"}).status_code == 200
    return client, rt


def test_status_has_speed_budget_and_demo_controls_refuse_cleanly(app):
    client, rt = app
    s = client.get("/api/status").json()
    assert s["fleet"] == [] and s["orders"] is None and s["alerts"] is None
    t = s["timing"]
    assert t["mode"] == "replay" and t["decision_ms"] > 0 and t["model"]["ms_per_headline"] >= 0
    assert s["demo"]["controls"] is True and isinstance(s["demo"]["cpp_bot_built"], bool)
    assert client.post("/api/bot/shadow", json={"on": True}).status_code == 409  # no example bot in this process
    assert client.post("/api/fleet/cpp-bot/1/stop").status_code == 404  # not a bot started here
    assert client.post("/api/fleet/cpp-bot/1/explode").status_code == 404
    assert client.post("/api/alerts/test").status_code == 409  # nothing configured


def test_alert_test_endpoint_sends_to_each_target(app, monkeypatch):
    client, rt = app
    monkeypatch.setenv("T_URL", "https://hooks.example/abc")
    seen = []
    rt.alerts = AlertDispatcher(AlertsConfig(webhooks=[WebhookConfig(kind="discord", url_env="T_URL")]),
                                lambda: rt.controller.monitor.status(), "replay",
                                client=httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(204))))
    try:
        r = client.post("/api/alerts/test").json()
        assert r["results"][0]["ok"] and "test alert" in json.loads(seen[0].content)["content"]
        assert client.get("/api/status").json()["alerts"]["targets"][0]["sent"] == 1
    finally:
        rt.alerts = None
