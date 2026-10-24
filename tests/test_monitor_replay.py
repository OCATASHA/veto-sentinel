"""Integration tests for the monitor over the synthetic fixture (fallback backends, no network)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from vetosentinel.config import Settings
from vetosentinel.engine.monitor import Monitor
from vetosentinel.evaluation.report import evaluate
from vetosentinel.features.backends import HashingEmbedder, LexiconSentiment
from vetosentinel.features.pipeline import FeaturePipeline
from vetosentinel.ingest.base import AdapterRunner, FetchResult
from vetosentinel.models import DataOrigin, RawItem, RiskState
from vetosentinel.service import Backends, ReplayController
from vetosentinel.store import Store
from vetosentinel.timeutil import SimClock

FIXTURE = "fixtures/synthetic_demo.jsonl"


def fallback_settings():
    s = Settings()
    s.panic.backend = "lexicon"
    s.drift.backend = "hashing"
    return s


@pytest.fixture(scope="module")
def replay():
    s = fallback_settings()
    store = Store(":memory:")
    c = ReplayController(s, FIXTURE, store, Backends(s))
    c.run_to_end()
    yield c, store
    store.close()


def states_between(store, run_id, a, b):
    return [r["state"] for r in store.query(
        "SELECT state FROM risk_scores WHERE run_id = ? AND as_of >= ? AND as_of < ? ORDER BY as_of",
        [run_id, a, b])]


def test_replay_is_labelled_synthetic_and_fallback(replay):
    c, store = replay
    st = c.monitor.status()
    assert st.data_origin == DataOrigin.SYNTHETIC.value
    assert all(v["is_fallback"] for k, v in st.implementations.items() if k != "volume")
    run = store.query("SELECT * FROM runs WHERE run_id = ?", [c.monitor.run_id])[0]
    assert run["data_origin"] == "synthetic_fixture" and run["parameter_status"] == "unvalidated starting parameters"
    assert json.loads(run["implementations"])["panic"]["implementation"] == "fallback-lexicon-v1"


def test_warmup_is_halt_not_normal(replay):
    c, store = replay
    assert set(states_between(store, c.monitor.run_id, "2026-02-02 00:00", "2026-02-04 00:00")) == {"HALT"}


def test_outage_never_normal(replay):
    c, store = replay
    s = states_between(store, c.monitor.run_id, "2026-02-09 02:30", "2026-02-09 05:00")
    assert s and "NORMAL" not in s


def test_stress_scenarios_escalate(replay):
    c, store = replay
    assert "HALT" in states_between(store, c.monitor.run_id, "2026-02-10 14:00", "2026-02-10 17:00")
    assert "HALT" in states_between(store, c.monitor.run_id, "2026-02-12 13:30", "2026-02-12 23:30")


def test_every_tick_audited_and_finite(replay):
    c, store = replay
    rid = c.monitor.run_id
    n_ticks = store.query("SELECT count(*) n FROM risk_scores WHERE run_id = ?", [rid])[0]["n"]
    n_dec = store.query("SELECT count(*) n FROM decisions WHERE run_id = ?", [rid])[0]["n"]
    assert n_ticks == n_dec == c.monitor.counters["ticks"]
    bad = store.query("""SELECT count(*) n FROM features WHERE run_id = ? AND (isnan(z) OR isinf(z)
                         OR isnan(raw) OR isinf(raw))""", [rid])[0]["n"]
    assert bad == 0
    t = store.transitions(rid)
    assert t and all(x["rule"] and x["reason"] for x in t)


def test_dedup_and_malformed_counted(replay):
    c, store = replay
    assert c.monitor.counters["duplicates"] > 200  # includes the deliberate syndicated copies
    assert c.monitor.counters["malformed"] == 3


def test_replay_is_deterministic():
    s = fallback_settings()
    out = []
    for _ in range(2):
        store = Store(":memory:")
        c = ReplayController(s, FIXTURE, store, Backends(s))
        c.seek(datetime(2026, 2, 6, tzinfo=UTC))
        out.append([(r["as_of"], r["state"], round(r["risk_score"] or -1, 9)) for r in
                    store.query("SELECT as_of, state, risk_score FROM risk_scores WHERE run_id = ? ORDER BY as_of",
                                [c.monitor.run_id])])
        store.close()
    assert out[0] == out[1]


def test_evaluate_writes_labelled_report(tmp_path):
    rep = evaluate(fallback_settings(), FIXTURE, str(tmp_path))
    assert rep["synthetic"] and "SYNTHETIC" in rep["disclaimer"]
    md = (tmp_path / "synthetic_demo_report.md").read_text()
    assert "SYNTHETIC DEMO DATA - NOT OBSERVED HISTORY" in md and "FALLBACK HEURISTIC" in md
    names = {s["name"] for s in rep["scenarios"]}
    assert names == {"feed_outage", "policy_surprise", "bank_stress"}


# ---------------------------------------------------------------- live-mode behaviour with fake adapters
class ScriptedAdapter:
    name = "scripted"
    data_origin = DataOrigin.LIVE

    def __init__(self):
        self.fail = False
        self.items = []

    def fetch(self, now):
        if self.fail:
            return FetchResult(ok=False, error="provider outage")
        items, self.items = self.items, []
        return FetchResult(ok=True, items=items)


def test_live_ingestion_outage_makes_monitor_not_ready():
    s = fallback_settings()
    s.ingest.stale_after_seconds = 600
    t0 = datetime(2026, 3, 2, tzinfo=UTC)
    clock = SimClock(t0)
    ad = ScriptedAdapter()
    mon = Monitor(s, Store(":memory:"), FeaturePipeline(s, LexiconSentiment(), HashingEmbedder(64)),
                  [AdapterRunner(ad, poll_seconds=60)], clock, mode="live", data_origin="live_provider")
    d = mon.tick()
    assert d.new_state is RiskState.HALT
    for i in range(30):
        clock.advance(timedelta(minutes=1))
        ad.items = [RawItem(headline=f"Story {i}", source="x", url=f"https://ex.com/{i}",
                            published_raw=clock.now().isoformat())]
        mon.tick()
    ad.fail = True
    for _ in range(20):
        clock.advance(timedelta(minutes=1))
        d = mon.tick()
    assert d.new_state is RiskState.HALT
    assert any("ingestion stale" in r for r in d.health["not_ready_reasons"])
    assert mon.status().adapters[0]["consecutive_failures"] >= 1


def test_operator_flatten_two_step_and_audited():
    s = fallback_settings()
    store = Store(":memory:")
    c = ReplayController(s, FIXTURE, store, Backends(s))
    c.seek(datetime(2026, 2, 5, 12, tzinfo=UTC))
    mon = c.monitor
    req = mon.request_flatten("alice", "drill")
    with pytest.raises(PermissionError):
        mon.confirm_flatten("bob", req["confirmation_code"])  # different operator
    with pytest.raises(PermissionError):
        mon.confirm_flatten("alice", "WRONG")
    req = mon.request_flatten("alice", "drill")
    d = mon.confirm_flatten("alice", req["confirmation_code"])
    assert d.new_state is RiskState.FLATTEN and mon.status().control["automatic_execution"] is False
    c.step()
    assert mon.sm.state is RiskState.FLATTEN
    mon.clear_flatten("alice", "drill over")
    assert mon.sm.state is RiskState.HALT
    kinds = [e["kind"] for e in store.control_events(mon.run_id)]
    assert {"FLATTEN_REQUESTED", "FLATTEN_CONFIRM_REJECTED", "FLATTEN_CONFIRMED", "FLATTEN_CLEARED"} <= set(kinds)
