"""Operator dashboard: FastAPI JSON API + static pages (no CDN, works offline).

Pages: ``/`` animated welcome then login (``/login`` skips the intro), ``/dashboard`` the
console (redirects to ``/login`` without a session). Every ``/api`` route except health,
session, login and logout requires a logged-in session (see ``auth.py``).

Security note (prototype): binds to 127.0.0.1 by default. If ``VETOSENTINEL_OPERATOR_TOKEN``
is set, operator and control endpoints additionally require header ``X-Operator-Token``
(the dashboard page does not send it, so its buttons become read-only).
"""

from __future__ import annotations

import hmac
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from vetosentinel.assistant import AssistantUnavailable, summary_facts
from vetosentinel.dashboard.auth import COOKIE_NAME, Auth
from vetosentinel.dashboard.compare import comparison
from vetosentinel.models import DataOrigin
from vetosentinel.runtime import CPP_BOT
from vetosentinel.timeutil import parse_iso_utc, utcnow

STATIC = Path(__file__).parent / "static"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class FlattenRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)


class FlattenConfirm(BaseModel):
    operator: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=16)


class FocusRequest(BaseModel):
    text: str = Field(default="", max_length=300)  # comma-separated phrases; "" = whole market
    background_weight: float | None = Field(default=None, ge=0, le=1)


class SpeedRequest(BaseModel):
    speed: float


class SeekRequest(BaseModel):
    to: str


class SummaryRequest(BaseModel):
    t: str | None = Field(default=None, max_length=64)  # a moment (ISO time); None = now


class SuggestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class ShadowRequest(BaseModel):
    on: bool


def _check_token(token: str | None) -> None:
    expected = os.environ.get("VETOSENTINEL_OPERATOR_TOKEN")
    if expected and not (token and hmac.compare_digest(token, expected)):
        raise HTTPException(401, "operator token required (X-Operator-Token)")


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def cooldown_json(rt: Any) -> dict[str, Any] | None:
    """Server-side calm countdown, so every viewer (and every refresh) sees the same running clock.

    Live, the calm period runs on the real clock, so ``remaining_s`` is already exact. In a replay
    it is as of the last replay step, and ``remaining_now_s`` adds how far the current step has got
    at replay speed. ``rate`` is how many seconds of it pass per real second, for smooth ticking in
    the browser between polls.
    """
    cd = rt.controller.monitor.cooldown_status()
    if cd is None:
        return None
    c = rt.controller
    if hasattr(c, "restart"):  # replay
        rate = c.speed if c.playing and not c.finished else 0.0
        progress = min(rate * (time.monotonic() - c.last_step_mono), c.tick.total_seconds()) if rate else 0.0
        remaining_now = max(0.0, cd["remaining_s"] - progress)
    else:
        rate, remaining_now = 1.0, cd["remaining_s"]
    return {**cd, "remaining_now_s": remaining_now, "rate": rate}


def timing_json(rt: Any) -> dict[str, Any]:
    """Measured time at each step from a news batch to a bot's order check (speed budget)."""
    mon = rt.controller.monitor
    live = not hasattr(rt.controller, "restart")
    snap = mon.status().snapshot
    fetches = [{"name": r.adapter.name, "seconds": r.health.last_duration_s, "items": r.health.last_new_items,
                "batch_minutes": 15 if getattr(r.adapter, "backfill_days", None) is not None else None}
               for r in mon.adapters if r.health.last_duration_s is not None]
    client = rt.demo_client.status() if rt.demo_client is not None else {}
    fleet = rt.fleet.snapshot() if rt.fleet is not None else []
    cpp = [b["check_ns"] for b in fleet if b["sdk"] == "cpp" and b["check_ns"] and b["status"] == "ok"]
    return {"mode": "live" if live else "replay",
            "data_lag_s": (utcnow() - snap.as_of).total_seconds() if live and snap is not None else None,
            "fetches": fetches, "model": mon.pipeline.model_cost(), "decision_ms": mon.tick_ms,
            "bus_latency_ms": client.get("bus_latency_ms"), "check_ns_python": client.get("check_ns"),
            "check_ns_cpp": min(cpp) if cpp else None}


def ai_json(rt: Any) -> dict[str, Any]:
    a = getattr(rt, "assistant", None)
    if a is None or not a.enabled:
        return {"enabled": False}
    info = a.info()
    return {"enabled": True, "provider": info["provider"], "model": info["model"]}


def moment_json(rt: Any, t: str) -> dict[str, Any]:
    """The stored assessment in force at ``t`` (ISO), with its headlines."""
    try:
        at = parse_iso_utc(t)
    except ValueError as e:
        raise HTTPException(400, f"bad timestamp: {e}") from e
    mon = rt.controller.monitor
    m = rt.store.moment(mon.run_id, at)
    if m is None:
        raise HTTPException(404, "no assessment at or before that time")
    heads = rt.store.headlines_by_id(mon.run_id, m["top_headline_ids"][: rt.settings.bus.top_headlines])
    for h in heads:
        h["relevance"] = mon.pipeline.relevance_of(h["headline_id"])
    m["headlines"] = heads
    m["trending"] = mon.trending_at(datetime.fromisoformat(m["as_of"]))
    return m


def summary_view(rt: Any, t: str | None) -> dict[str, Any]:
    """What the AI summary is written from: one moment (``t``) or the current assessment."""
    focus = rt.controller.monitor.focus_info().get("text", "")
    if t:
        m = moment_json(rt, t)
        return {"state": m["state"], "as_of": m["as_of"], "risk_score": m["risk_score"],
                "thresholds": rt.settings.state_machine.model_dump(),
                "z": {"panic": m["panic_z"], "drift": m["drift_z"], "volume": m["volume_z"]},
                "cusum_alarm": m["cusum_alarm_active"],
                "problems": m["degraded_reasons"] + ([] if m["monitor_ready"] else m["not_ready_reasons"]),
                "focus": focus, "trending": [w["word"] for w in m["trending"]], "headlines": m["headlines"]}
    st = status_json(rt)
    cp = (st["decision"] or {}).get("changepoint") or {}
    return {"state": st["state"], "as_of": st["as_of"], "risk_score": st["risk_score"],
            "thresholds": st["thresholds"],
            "z": {k: (c.get("z") if c.get("available") else None) for k, c in st["components"].items()},
            "cusum_alarm": bool(cp.get("alarm_active")),
            "problems": st["degraded_reasons"] + ([] if st["monitor_ready"] else st["not_ready_reasons"]),
            "focus": focus, "trending": [w["word"] for w in st.get("trending") or []],
            "headlines": st["top_headlines"]}


def status_json(rt: Any) -> dict[str, Any]:
    mon = rt.controller.monitor
    st = mon.status()
    snap = st.snapshot
    s = rt.settings
    top = rt.store.headlines_by_id(mon.run_id, [h["headline_id"] for h in st.top_headlines])
    for h in top:
        h["relevance"] = mon.pipeline.relevance_of(h["headline_id"])
    d = st.decision
    return {
        "state": str(st.state), "reason": st.reason, "run_id": st.run_id, "mode": st.mode,
        "data_origin": st.data_origin, "synthetic": st.data_origin == DataOrigin.SYNTHETIC.value,
        "parameter_status": s.parameter_status,
        "as_of": snap.as_of.isoformat() if snap else None,
        "risk_score": snap.risk_score if snap else None,
        "monitor_ready": snap.monitor_ready if snap else False,
        "not_ready_reasons": snap.not_ready_reasons if snap else ["no assessment yet"],
        "degraded": snap.degraded if snap else True,
        "degraded_reasons": snap.degraded_reasons if snap else [],
        "components": {k: v.model_dump() for k, v in snap.components.items()} if snap else {},
        "top_headlines": top,
        "implementations": st.implementations, "model_notes": st.model_notes,
        "fallback_in_use": any(v["is_fallback"] for v in st.implementations.values()),
        "adapters": st.adapters, "counters": st.counters, "flatten": st.flatten, "control": st.control,
        "decision": None if d is None else {
            "rule": d.rule, "reason": d.reason, "as_of": d.as_of.isoformat(),
            "changepoint": d.changepoint.model_dump() if d.changepoint else None},
        "thresholds": s.state_machine.model_dump(), "weights": s.composite.weights,
        "cusum": s.cusum.model_dump(),
        "replay": rt.controller.info(),
        "publisher": None if rt.publisher is None else {
            "endpoint": s.bus.pub_endpoint, "topic": s.bus.topic, "publisher_id": rt.publisher.publisher_id,
            "seq": rt.publisher.seq, "sent": rt.publisher.sent, "error": rt.publisher.error,
            "heartbeat_interval_ms": s.bus.heartbeat_interval_ms,
            "heartbeat_timeout_ms": s.bus.heartbeat_timeout_ms,
            "fault_injection": s.bus.allow_fault_injection, "injected": rt.publisher.injected[-10:]},
        "client": rt.demo_client.status() if rt.demo_client is not None else None,
        "cooldown": cooldown_json(rt),
        "relax": mon.exit_checklist(),
        "trending": mon.trending(),
        "focus": st.focus,
        "ai": ai_json(rt),
        "fleet": rt.fleet.snapshot() if getattr(rt, "fleet", None) is not None else [],
        "orders": rt.orders.to_json() if getattr(rt, "orders", None) is not None else None,
        "timing": timing_json(rt),
        "alerts": rt.alerts.info() if getattr(rt, "alerts", None) is not None else None,
        "demo": {"controls": s.bus.allow_fault_injection, "cpp_bot_built": CPP_BOT.exists(),
                 "cpp_bots": [pid for pid, p in getattr(rt, "bots", {}).items() if p.poll() is None]},
        "caution_position_fraction": (rt.demo_client.tracker.cfg.caution_position_fraction
                                      if rt.demo_client is not None else 0.25),
    }


def create_app(rt: Any, auth: Auth | None = None) -> FastAPI:
    auth = auth or Auth.from_env()
    app = FastAPI(title="Veto Sentinel", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            # Pages and static files: always revalidate (cheap 304 via ETag), so a normal refresh
            # never mixes an old cached page with a newer script after an update.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def session_user(request: Request) -> str | None:
        return auth.sessions.user_for(request.cookies.get(COOKIE_NAME))

    def require_user(request: Request) -> str:
        user = session_user(request)
        if user is None:
            raise HTTPException(401, "login required")
        return user

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------------ public pages + auth
    @app.get("/")
    def welcome() -> FileResponse:
        return FileResponse(STATIC / "welcome.html")

    @app.get("/login")
    def login_page() -> FileResponse:
        return FileResponse(STATIC / "welcome.html")

    @app.get("/dashboard", response_model=None)
    def dashboard(request: Request) -> FileResponse | RedirectResponse:
        if session_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request) -> dict:
        user = session_user(request)
        return {"authenticated": user is not None, "user": user}

    @app.post("/api/login")
    def login(req: LoginRequest, request: Request, response: Response) -> dict:
        key = request.client.host if request.client else "unknown"
        wait = auth.limiter.retry_after(key)
        if wait > 0:
            secs = math.ceil(wait)
            raise HTTPException(429, f"Too many failed attempts. Try again in {secs} s.",
                                headers={"Retry-After": str(secs)})
        if not auth.credentials.verify(req.username, req.password):
            auth.limiter.failure(key)
            raise HTTPException(401, "Wrong username or password.")
        auth.limiter.success(key)
        token = auth.sessions.create(auth.credentials.username)
        response.set_cookie(COOKIE_NAME, token, max_age=int(auth.sessions.ttl.total_seconds()), path="/",
                            httponly=True, samesite="strict", secure=_is_https(request))
        return {"ok": True, "user": auth.credentials.username}

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict:
        auth.sessions.revoke(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    # ------------------------------------------------------------------ data (login required)
    api = APIRouter(dependencies=[Depends(require_user)])

    @api.get("/api/status")
    def status() -> dict:
        return status_json(rt)

    @api.get("/api/history")
    def history(limit: int = 4000, since: str | None = None) -> list[dict]:
        run_id = rt.controller.monitor.run_id
        limit = max(1, min(limit, 20000))
        if since:
            try:
                after = parse_iso_utc(since)
            except ValueError as e:
                raise HTTPException(400, f"bad timestamp: {e}") from e
            return rt.store.score_history_since(run_id, after, limit)
        return rt.store.score_history(run_id, limit)

    @api.get("/api/transitions")
    def transitions(limit: int = 100) -> list[dict]:
        return rt.store.transitions(rt.controller.monitor.run_id, max(1, min(limit, 1000)))

    @api.get("/api/moment")
    def moment(t: str) -> dict:
        return moment_json(rt, t)

    @api.get("/api/compare")
    def compare() -> dict:
        path = getattr(rt.controller, "fixture_path", None)
        return comparison(path) if path else {"runs": []}

    @api.get("/api/control-events")
    def control_events() -> list[dict]:
        return rt.store.control_events(rt.controller.monitor.run_id)

    # ------------------------------------------------------------------ replay controls
    def _replay():
        if not hasattr(rt.controller, "restart"):
            raise HTTPException(409, "not in replay mode")
        return rt.controller

    @api.post("/api/replay/{action}")
    def replay_action(action: str, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        c = _replay()
        if action == "play":
            c.play()
        elif action == "pause":
            c.pause()
        elif action == "restart":
            c.restart()
        elif action == "step":
            c.step()
        else:
            raise HTTPException(404, f"unknown action {action}")
        return c.info()

    @api.post("/api/replay-speed")
    def replay_speed(req: SpeedRequest, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        c = _replay()
        try:
            c.set_speed(req.speed)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return c.info()

    @api.post("/api/replay-seek")
    def replay_seek(req: SeekRequest, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        c = _replay()
        try:
            target = parse_iso_utc(req.to)
        except ValueError as e:
            raise HTTPException(400, f"bad timestamp: {e}") from e
        if target < c.clock.now():
            raise HTTPException(400, "seek is forward-only (restart to go back)")
        n = c.seek(target)
        return {"ticks": n, **c.info()}

    # ------------------------------------------------------------------ AI assistant (advisory text only)
    def _assistant():
        a = getattr(rt, "assistant", None)
        if a is None or not a.enabled:
            raise HTTPException(409, "The AI assistant is off: set GEMINI_API_KEY to turn it on.")
        return a

    def _ai(fn, *args):
        try:
            return fn(*args)
        except AssistantUnavailable as e:
            if e.retry_after:
                raise HTTPException(429, str(e), headers={"Retry-After": str(math.ceil(e.retry_after))}) from e
            raise HTTPException(503, str(e)) from e

    @api.post("/api/ai/summary")
    def ai_summary(req: SummaryRequest) -> dict:
        a = _assistant()
        view = summary_view(rt, req.t)
        out = _ai(a.summarize, summary_facts(view))
        return {**out, "as_of": view["as_of"], "state": view["state"],
                "headlines": [{"n": i + 1, "headline": h.get("headline")} for i, h in enumerate(view["headlines"])]}

    @api.post("/api/ai/focus-suggest")
    def ai_focus_suggest(req: SuggestRequest) -> dict:
        # From the topic alone: recent "best matches" can be weak (e.g. OPEC during a bank crisis)
        # and pull the suggestions off topic. The preview then shows what the phrases would match.
        return _ai(_assistant().suggest_phrases, req.text)

    # ------------------------------------------------------------------ focus ("Optimize for")
    @api.post("/api/focus/preview")
    def focus_preview(req: FocusRequest) -> dict:
        try:
            return rt.controller.monitor.preview_focus(req.text, req.background_weight)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @api.post("/api/focus")
    def focus_set(req: FocusRequest, user: str = Depends(require_user),
                  x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        try:
            return rt.controller.monitor.set_focus(req.text, req.background_weight, operator=user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ------------------------------------------------------------------ operator FLATTEN (two-step)
    @api.post("/api/operator/flatten/request")
    def flatten_request(req: FlattenRequest, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        return rt.controller.monitor.request_flatten(req.operator, req.reason)

    @api.post("/api/operator/flatten/confirm")
    def flatten_confirm(req: FlattenConfirm, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        try:
            d = rt.controller.monitor.confirm_flatten(req.operator, req.code)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return {"state": str(d.new_state), "reason": d.reason}

    @api.post("/api/operator/flatten/clear")
    def flatten_clear(req: FlattenRequest, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        try:
            d = rt.controller.monitor.clear_flatten(req.operator, req.reason)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"state": str(d.new_state), "reason": d.reason}

    # ------------------------------------------------------------------ webhook alerts
    @api.post("/api/alerts/test")
    def alerts_test(x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        a = getattr(rt, "alerts", None)
        if a is None or not a.active:
            raise HTTPException(409, "no alert targets configured (set e.g. VETOSENTINEL_NTFY_URL and restart)")
        return {"results": a.test()}

    # ------------------------------------------------------------------ demo bots (fleet view)
    def _demo_controls() -> None:
        if not rt.settings.bus.allow_fault_injection:
            raise HTTPException(403, "demo controls are turned off (bus.allow_fault_injection = false)")

    @api.post("/api/bot/shadow")
    def bot_shadow(req: ShadowRequest, x_operator_token: str | None = Header(default=None)) -> dict:
        """Switch the dashboard's example bot in or out of shadow mode (demo only)."""
        _check_token(x_operator_token)
        _demo_controls()
        if rt.demo_client is None:
            raise HTTPException(409, "no example bot running")
        rt.demo_client.tracker.cfg.shadow = req.on
        return {"shadow": req.on}

    @api.post("/api/fleet/cpp-bot")
    def cpp_bot_start(x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        try:
            return rt.spawn_cpp_bot()
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except (FileNotFoundError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from e

    @api.post("/api/fleet/cpp-bot/{pid}/{how}")
    def cpp_bot_stop(pid: int, how: str, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        _demo_controls()
        if how not in ("stop", "kill"):
            raise HTTPException(404, "how must be stop or kill")
        try:
            return rt.stop_cpp_bot(pid, hard=how == "kill")
        except KeyError as e:
            raise HTTPException(404, "not a bot started from this dashboard") from e

    # ------------------------------------------------------------------ demo fault injection
    @api.post("/api/fault/{fault}")
    def fault(fault: str, x_operator_token: str | None = Header(default=None)) -> dict:
        _check_token(x_operator_token)
        if rt.publisher is None:
            raise HTTPException(409, "no publisher running")
        try:
            rt.publisher.inject(fault)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return {"injected": fault}

    app.include_router(api)
    return app
