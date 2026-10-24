"""Dashboard login: credentials, sessions, rate limiting, route protection and headers."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from vetosentinel.config import Settings
from vetosentinel.dashboard.app import create_app
from vetosentinel.dashboard.auth import (
    COOKIE_NAME,
    DEFAULT_USER,
    Auth,
    Credentials,
    LoginLimiter,
    SessionStore,
)
from vetosentinel.runtime import Runtime
from vetosentinel.service import Backends, ReplayController
from vetosentinel.store import Store

USER, PASSWORD = "tester", "correct horse battery"


@pytest.fixture(scope="module")
def runtime():
    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    store = Store(":memory:")
    rt = Runtime(s, store, ReplayController(s, "fixtures/synthetic_demo.jsonl", store, Backends(s)))
    rt.controller.step()
    yield rt
    store.close()


@pytest.fixture
def client(runtime):
    return TestClient(create_app(runtime, auth=Auth(Credentials.from_password(USER, PASSWORD))))


def login(c, user=USER, password=PASSWORD):
    return c.post("/api/login", json={"username": user, "password": password})


# ---------------------------------------------------------------- credentials
def test_default_account_is_oxhack26_and_rejects_wrong_passwords(monkeypatch):
    monkeypatch.delenv("VETOSENTINEL_LOGIN_USER", raising=False)
    monkeypatch.delenv("VETOSENTINEL_LOGIN_PASSWORD", raising=False)
    creds = Credentials.from_env()
    assert creds.username == DEFAULT_USER == "oxhack26"
    assert not creds.verify("oxhack26", "wrong")
    assert not creds.verify("oxhack26", "")
    assert len(creds.digest) == 32 and len(creds.salt) == 16  # stored as a salted hash only


def test_env_overrides_credentials(monkeypatch):
    monkeypatch.setenv("VETOSENTINEL_LOGIN_USER", "alice")
    monkeypatch.setenv("VETOSENTINEL_LOGIN_PASSWORD", "s3cret-pass")
    creds = Credentials.from_env()
    assert creds.verify("alice", "s3cret-pass")
    assert not creds.verify("oxhack26", "s3cret-pass")
    assert not creds.verify("alice", "S3cret-pass")


def test_username_and_password_must_both_match():
    creds = Credentials.from_password("bob", "pw")
    assert creds.verify("bob", "pw")
    assert not creds.verify("Bob", "pw")
    assert not creds.verify("bob", "pw ")


# ---------------------------------------------------------------- sessions + limiter
def test_sessions_expire_revoke_and_cap():
    store = SessionStore(ttl=timedelta(hours=1), max_sessions=2)
    a = store.create("u")
    assert store.user_for(a) == "u" and store.user_for("nope") is None and store.user_for(None) is None
    store.revoke(a)
    assert store.user_for(a) is None
    b, c, d = store.create("u"), store.create("u"), store.create("u")
    assert store.user_for(b) is None and store.user_for(c) == "u" and store.user_for(d) == "u"  # oldest evicted
    expired = SessionStore(ttl=timedelta(seconds=0))
    assert expired.user_for(expired.create("u")) is None


def test_limiter_locks_after_repeated_failures():
    lim = LoginLimiter(max_failures=3, window_s=60, lockout_s=30)
    for t in (0, 1, 2):
        assert lim.retry_after("ip", now=t) == 0
        lim.failure("ip", now=t)
    assert lim.retry_after("ip", now=3) == pytest.approx(29)
    assert lim.retry_after("other", now=3) == 0
    assert lim.retry_after("ip", now=70) == 0  # failures aged out of the window
    lim.failure("ip2", now=0)
    lim.success("ip2")
    assert lim.retry_after("ip2", now=1) == 0


# ---------------------------------------------------------------- routes
def test_api_requires_login(client):
    for path in ("/api/status", "/api/history", "/api/transitions", "/api/control-events"):
        r = client.get(path)
        assert r.status_code == 401 and r.json()["detail"] == "login required", path
    for path in ("/api/replay/play", "/api/fault/malformed", "/api/operator/flatten/clear"):
        assert client.post(path, json={"operator": "x", "reason": "y"}).status_code == 401, path
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/session").json() == {"authenticated": False, "user": None}


def test_pages(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/", "/login"):
        r = client.get(path)
        assert r.status_code == 200 and "Veto Sentinel" in r.text and "login-form" in r.text
    for asset in ("welcome.js", "welcome.css", "dashboard.js", "dashboard.css", "theme.js", "favicon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200, asset
    for path in ("/docs", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_login_flow(client):
    assert login(client, password="nope").status_code == 401
    assert login(client, user="TESTER").status_code == 401
    assert client.post("/api/login", json={"username": "", "password": ""}).status_code == 422
    r = login(client)
    assert r.status_code == 200 and r.json() == {"ok": True, "user": USER}
    cookie = r.headers["set-cookie"].lower()
    assert f"{COOKIE_NAME}=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert client.get("/api/session").json() == {"authenticated": True, "user": USER}
    assert client.get("/api/status").status_code == 200
    page = client.get("/dashboard")
    assert page.status_code == 200 and "dashboard.js" in page.text
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_cookie_secure_behind_https_proxy(client):
    r = client.post("/api/login", json={"username": USER, "password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


def test_forged_cookie_is_rejected(client):
    client.cookies.set(COOKIE_NAME, "forged-token")
    assert client.get("/api/status").status_code == 401


def test_rate_limit(runtime):
    auth = Auth(Credentials.from_password(USER, PASSWORD), limiter=LoginLimiter(max_failures=3, window_s=300, lockout_s=60))
    c = TestClient(create_app(runtime, auth=auth))
    for _ in range(3):
        assert login(c, password="bad").status_code == 401
    r = login(c)  # even the right password is refused during the lockout
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert "Too many failed attempts" in r.json()["detail"]


def test_security_headers(client):
    for path in ("/", "/api/health"):
        h = client.get(path).headers
        assert "script-src 'self'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert client.get("/api/health").headers["cache-control"] == "no-store"
