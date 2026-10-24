import io
import zipfile
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from vetosentinel.config import DriftConfig, Settings
from vetosentinel.dashboard.app import create_app
from vetosentinel.dashboard.auth import Auth, Credentials
from vetosentinel.evaluation.gdelt_gkg import file_times, parse_gkg
from vetosentinel.features.drift import DriftTracker
from vetosentinel.runtime import Runtime
from vetosentinel.service import Backends, ReplayController
from vetosentinel.store import Store

T0 = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def api():
    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    store = Store(":memory:")
    ctrl = ReplayController(s, "fixtures/synthetic_demo.jsonl", store, Backends(s))
    rt = Runtime(s, store, ctrl)  # no publisher / analytics thread: drive the controller directly
    ctrl.seek(datetime(2026, 2, 5, 12, tzinfo=UTC))
    client = TestClient(create_app(rt, auth=Auth(Credentials.from_password("tester", "pw-for-tests"))))
    assert client.post("/api/login", json={"username": "tester", "password": "pw-for-tests"}).status_code == 200
    yield client, rt


def test_status_exposes_provenance_and_fallbacks(api):
    client, rt = api
    s = client.get("/api/status").json()
    assert s["synthetic"] and s["data_origin"] == "synthetic_fixture"
    assert s["fallback_in_use"] and s["parameter_status"] == "unvalidated starting parameters"
    assert set(s["components"]) == {"panic", "drift", "volume"}
    assert s["state"] in ("NORMAL", "CAUTION", "HALT")
    assert s["caution_position_fraction"] == 0.25
    assert client.get("/").status_code == 200


def test_history_since_is_incremental(api):
    client, _ = api
    full = client.get("/api/history?limit=100").json()
    cut = full[-10]["as_of"]
    after = client.get("/api/history", params={"since": cut}).json()
    assert [r["as_of"] for r in after] == [r["as_of"] for r in full[-9:]]
    assert client.get("/api/history", params={"since": "not-a-time"}).status_code == 400


def test_history_and_transitions(api):
    client, _ = api
    h = client.get("/api/history?limit=50").json()
    assert len(h) == 50 and h[0]["as_of"] < h[-1]["as_of"]
    t = client.get("/api/transitions").json()
    assert t and {"prev_state", "new_state", "rule", "reason"} <= set(t[0])


def test_replay_controls(api):
    client, rt = api
    before = rt.controller.clock.now()
    assert client.post("/api/replay/step").status_code == 200
    assert rt.controller.clock.now() > before
    assert client.post("/api/replay-speed", json={"speed": 0.1}).status_code == 400
    assert client.post("/api/replay-seek", json={"to": "2020-01-01T00:00:00Z"}).status_code == 400
    r = client.post("/api/replay-seek", json={"to": (rt.controller.clock.now() + timedelta(hours=1)).isoformat()})
    assert r.status_code == 200 and r.json()["ticks"] == 12
    assert client.post("/api/replay/bogus").status_code == 404


def test_flatten_flow_and_token(api, monkeypatch):
    client, rt = api
    r = client.post("/api/operator/flatten/request", json={"operator": "op", "reason": "drill"}).json()
    assert client.post("/api/operator/flatten/confirm", json={"operator": "op", "code": "nope"}).status_code == 403
    r = client.post("/api/operator/flatten/confirm", json={"operator": "op", "code": r["confirmation_code"]})
    assert r.status_code == 200 and r.json()["state"] == "FLATTEN"
    assert client.get("/api/status").json()["state"] == "FLATTEN"
    assert client.post("/api/operator/flatten/clear", json={"operator": "op", "reason": "done"}).json()["state"] == "HALT"
    assert client.post("/api/operator/flatten/clear", json={"operator": "op", "reason": "x"}).status_code == 409
    kinds = {e["kind"] for e in client.get("/api/control-events").json()}
    assert {"FLATTEN_REQUESTED", "FLATTEN_CONFIRMED", "FLATTEN_CLEARED"} <= kinds
    monkeypatch.setenv("VETOSENTINEL_OPERATOR_TOKEN", "s3cret")
    assert client.post("/api/replay/step").status_code == 401
    assert client.post("/api/replay/step", headers={"X-Operator-Token": "s3cret"}).status_code == 200
    assert client.post("/api/fault/malformed", headers={"X-Operator-Token": "s3cret"}).status_code == 409


# ---------------------------------------------------------------- drift bias correction
def _mean_drift(rng, bias_correction, n_recent, trials=40, noise=0.3):
    center = np.zeros(32)
    center[0] = 1.0
    vals = []
    for _ in range(trials):
        d = DriftTracker(DriftConfig(min_recent=3, min_reference=10, bias_correction=bias_correction), 32)
        d.add_many([(T0 + timedelta(minutes=20 * i), center + rng.normal(0, noise, 32)) for i in range(300)])
        t = T0 + timedelta(minutes=20 * 300)
        d.add_many([(t + timedelta(seconds=30 * i + 1), center + rng.normal(0, noise, 32)) for i in range(n_recent)])
        vals.append(d.compute(t + timedelta(minutes=59)).value)
    return float(np.mean(vals))


def test_drift_bias_correction_removes_sample_size_effect():
    """Same topic distribution in both windows: the expected corrected D should not depend on n."""
    rng = np.random.default_rng(3)
    raw_small, raw_big = _mean_drift(rng, False, 6), _mean_drift(rng, False, 60)
    cor_small, cor_big = _mean_drift(rng, True, 6), _mean_drift(rng, True, 60)
    assert raw_small - raw_big > 0.1  # uncorrected: small windows look "far away"
    assert abs(cor_small - cor_big) < 0.05


# ---------------------------------------------------------------- GKG parsing (offline)
def _gkg_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", "\n".join("\t".join(r) for r in rows))
    return buf.getvalue()


def _row(date, themes, title, url="https://ex.com/a", src="ex.com"):
    r = [""] * 27
    r[1], r[3], r[4], r[7] = date, src, url, themes
    r[26] = f"<PAGE_PRECISEPUB>1</PAGE_PRECISEPUB><PAGE_TITLE>{title}</PAGE_TITLE>"
    return r


def test_parse_gkg_filters_themes_and_titles():
    data = _gkg_zip([
        _row("20230310150000", "ECON_STOCKMARKET;TAX_FNCACT", "Bank shares slide as deposit fears grow"),
        _row("20230310150000", "EPU_POLICY;SPORTS", "Local team wins the cup final on penalties"),
        _row("20230310150000", "WB_1235_CENTRAL_BANKS", "short"),
        _row("bad", "ECON_STOCKMARKET", "Valid title but invalid date here"),
        ["too", "few", "cols"],
    ])
    rows = parse_gkg(data, ("ECON_STOCKMARKET", "WB_1235_CENTRAL_BANKS"))
    assert [r["headline"] for r in rows] == ["Bank shares slide as deposit fears grow"]
    assert rows[0]["published_at"] == "2023-03-10T15:00:00Z"


def test_gkg_file_times_sampling():
    ts = file_times(datetime(2023, 3, 10, 0, 7, tzinfo=UTC), datetime(2023, 3, 10, 2, tzinfo=UTC), 4)
    assert ts == [datetime(2023, 3, 10, 0, 0, tzinfo=UTC), datetime(2023, 3, 10, 1, 0, tzinfo=UTC)]


# ---------------------------------------------------------------- server-side calm countdown
def test_calm_countdown_is_kept_by_the_server():
    from vetosentinel.dashboard.app import cooldown_json

    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    ctrl = ReplayController(s, "fixtures/synthetic_demo.jsonl", Store(":memory:"), Backends(s))
    rt = Runtime(s, ctrl.store, ctrl)
    ctrl.seek(datetime(2026, 2, 10, 16, 55, tzinfo=UTC))  # cooling off after the invented rate shock
    cd = cooldown_json(rt)
    assert cd is not None and cd["next_state"] == "CAUTION" and cd["rate"] == 0  # paused: clock stands still
    assert cd["remaining_now_s"] == cd["remaining_s"] < cd["total_s"]
    ctrl.speed, ctrl.playing = 600.0, True  # playing: the server advances it between replay steps
    ctrl.last_step_mono -= 0.25
    cd2 = cooldown_json(rt)
    assert cd2 is not None and cd2["rate"] == 600 and cd2["remaining_s"] - cd2["remaining_now_s"] == pytest.approx(150, abs=5)
    # status JSON carries the same object, so a page refresh shows the running value
    status = TestClient(create_app(rt, auth=Auth(Credentials.from_password("u", "p"))))
    status.post("/api/login", json={"username": "u", "password": "p"})
    assert status.get("/api/status").json()["cooldown"]["started"] == cd["started"]


# ---------------------------------------------------------------- "Optimize for"
def test_optimize_for_preview_apply_audit_and_restart():
    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    ctrl = ReplayController(s, "fixtures/synthetic_demo.jsonl", Store(":memory:"), Backends(s))
    rt = Runtime(s, ctrl.store, ctrl)
    ctrl.seek(datetime(2026, 2, 10, 12, 0, tzinfo=UTC))
    c = TestClient(create_app(rt, auth=Auth(Credentials.from_password("u", "p"))))
    assert c.post("/api/focus/preview", json={"text": "banks"}).status_code == 401
    c.post("/api/login", json={"username": "u", "password": "p"})
    pv = c.post("/api/focus/preview", json={"text": "bank run, deposits", "background_weight": 0.1}).json()
    assert pv["phrases"] == ["bank run", "deposits"] and pv["is_fallback"]  # keyword fallback (no MiniLM)
    assert 0 < pv["effect"]["share"] < 1 and pv["top"] and all(0 < t["relevance"] <= 1 for t in pv["top"])
    assert c.get("/api/status").json()["focus"]["text"] == ""  # previewing changes nothing
    before = len(c.get("/api/history").json())
    r = c.post("/api/focus", json={"text": "bank run, deposits", "background_weight": 0.1})
    assert r.status_code == 200 and r.json()["set_by"] == "u" and r.json()["recompute_seconds"] >= 0
    st = c.get("/api/status").json()
    assert st["focus"]["text"] == "bank run, deposits" and st["focus"]["background_weight"] == 0.1
    assert st["focus"]["effect"]["focus_multiplier"] > 1
    assert all(h["relevance"] is not None for h in st["top_headlines"])
    assert len(c.get("/api/history").json()) == before + 1  # re-assessed straight away
    assert [e["kind"] for e in c.get("/api/control-events").json()][0] == "FOCUS_SET"
    assert c.post("/api/focus", json={"text": "the, and"}).status_code == 400  # nothing to match
    assert c.post("/api/focus", json={"text": "oil", "background_weight": 2}).status_code == 422
    c.post("/api/replay/restart")
    assert c.get("/api/status").json()["focus"]["text"] == "bank run, deposits"  # kept on restart
    c.post("/api/focus", json={"text": ""})
    assert c.get("/api/status").json()["focus"]["text"] == "" and st["focus"]["is_fallback"]
