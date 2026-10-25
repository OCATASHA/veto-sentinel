"""Webhook alerts on level changes and operator actions (outbound only).

Targets come from ``[[alerts.webhooks]]``: ``kind`` json | slack | discord | ntfy, and a URL given
directly or, better, through an environment variable (``url_env``) so secrets and private topics
stay out of config files. A target without a URL is skipped: nothing is sent unless someone sets one.

The monitor calls ``notify(kind)`` (a listener, like the bus publisher) and the event is queued;
one background thread sends it, never the analytics loop. Each target gets at most one alert per
``min_interval_seconds``: changes in between are folded into the next alert ("+2 more changes"),
so a fast replay cannot flood a phone. Sends have a short timeout and two retries.

JSON targets receive the full event and can be signed: with ``secret_env`` set, the request carries
``X-VetoSentinel-Timestamp: <unix seconds>`` and ``X-VetoSentinel-Signature: sha256=<hex>``, the
HMAC-SHA256 of ``<timestamp>.<body>``. Alerts never execute anything: ``automatic_execution`` is
always false, as on the bus.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from vetosentinel.config import AlertsConfig, WebhookConfig
from vetosentinel.engine.monitor import MonitorStatus
from vetosentinel.ingest.base import USER_AGENT
from vetosentinel.timeutil import iso_ms, utcnow

log = logging.getLogger(__name__)

TITLES = {"NORMAL": "All clear", "CAUTION": "Be careful", "HALT": "Stop", "FLATTEN": "Emergency"}
NTFY = {"NORMAL": ("3", "white_check_mark"), "CAUTION": ("4", "warning"), "HALT": ("5", "rotating_light"),
        "FLATTEN": ("5", "rotating_light,sos")}
SIGNALS = (("panic_z", "negativity"), ("drift_z", "topic change"), ("volume_z", "news volume"))


def event_from_status(kind: str, st: MonitorStatus, previous: str | None, mode: str) -> dict[str, Any]:
    snap = st.snapshot
    comps: dict[str, float | None] = {k: None for k, _ in SIGNALS}
    if snap is not None:
        for k, _ in SIGNALS:
            c = snap.components.get(k.removesuffix("_z"))
            comps[k] = round(c.z, 2) if c is not None and c.available and c.z is not None else None
    return {
        "event": "operator" if kind == "CONTROL" else "level_change", "state": str(st.state),
        "previous_state": previous, "risk_score": None if snap is None or snap.risk_score is None else round(snap.risk_score, 2),
        "as_of": iso_ms(snap.as_of) if snap is not None else None, "ts": iso_ms(utcnow()), "reason": st.reason[:500],
        "components": comps, "focus": st.focus.get("text") or None, "data_origin": st.data_origin, "mode": mode,
        "control": st.control, "automatic_execution": False,
    }


def event_test() -> dict[str, Any]:
    """A clearly marked test event (no monitor needed), e.g. for the preflight check."""
    return {"event": "test", "state": "NORMAL", "previous_state": None, "risk_score": None, "as_of": None,
            "ts": iso_ms(utcnow()), "reason": "test alert", "components": {k: None for k, _ in SIGNALS},
            "focus": None, "data_origin": "test", "mode": "test", "control": None, "automatic_execution": False}


def text_of(ev: dict[str, Any], folded: int, dashboard_url: str) -> tuple[str, str]:
    """(title, body) in plain words, for chat and phone targets."""
    if ev["event"] == "test":
        return "Veto Sentinel: test alert", ("Test alert from Veto Sentinel. Nothing has changed: if you can read "
                                             "this, level alerts will reach you here." + (f"\n{dashboard_url}" if dashboard_url else ""))
    level = TITLES.get(ev["state"], ev["state"])
    title = f"Veto Sentinel: {level}"
    lines = []
    if ev["event"] == "operator" and ev.get("control"):
        c = ev["control"]
        lines.append(f"Operator action {c.get('event')} by {c.get('operator')}"
                     + (f": {c['reason']}" if c.get("reason") else "") + ".")
    else:
        was = TITLES.get(ev["previous_state"] or "", ev["previous_state"] or "start-up")
        score = "n/a" if ev["risk_score"] is None else f"{ev['risk_score']:.2f}"
        lines.append(f"Level {level} (was {was}). Risk score {score}.")
    hot = [f"{name} z {ev['components'][k]:.1f}" for k, name in SIGNALS
           if ev["components"].get(k) is not None and ev["components"][k] >= 2]
    if hot:
        lines.append("Unusual: " + ", ".join(hot) + ".")
    if ev.get("focus"):
        lines.append(f"Optimized for: {ev['focus']}.")
    when = ev["as_of"] or ev["ts"]
    try:
        when = datetime.fromisoformat(when.replace("Z", "+00:00")).strftime("%a %d %b %H:%M UTC")
    except (ValueError, AttributeError):
        pass
    origin = {"live_provider": "live news", "historical_dataset": "replay of real past news",
              "synthetic_fixture": "replay of INVENTED demo news"}.get(ev["data_origin"], ev["data_origin"])
    lines.append(f"News time {when} ({origin}" + (", simulated clock" if ev["mode"] == "replay" else "") + ").")
    if folded:
        lines.append(f"(+{folded} earlier change{'s' if folded > 1 else ''} since the last alert)")
    if dashboard_url:
        lines.append(dashboard_url)
    lines.append("Veto Sentinel never trades: bots act on the level themselves.")
    return title, "\n".join(lines)


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


class Target:
    def __init__(self, cfg: WebhookConfig):
        self.cfg = cfg
        self.url = (os.environ.get(cfg.url_env, "") if cfg.url_env else cfg.url).strip()
        self.secret = os.environ.get(cfg.secret_env, "") if cfg.secret_env else ""
        self.pending: dict[str, Any] | None = None
        self.folded = 0
        self.next_at = 0.0
        self.sent = 0
        self.failed = 0
        self.last_result = ""
        self.last_at: str | None = None

    def info(self) -> dict[str, Any]:
        host = urlsplit(self.url).hostname if self.url else None  # never the full URL: it is the secret
        return {"kind": self.cfg.kind, "name": self.cfg.name or self.cfg.kind, "host": host,
                "configured": bool(self.url), "env": self.cfg.url_env or None, "signed": bool(self.secret),
                "sent": self.sent, "failed": self.failed, "last_result": self.last_result, "last_at": self.last_at}

    def request(self, ev: dict[str, Any], folded: int, dashboard_url: str) -> tuple[dict[str, str], bytes]:
        title, text = text_of(ev, folded, dashboard_url)
        k = self.cfg.kind
        if k == "ntfy":
            prio, tags = NTFY.get(ev["state"], ("3", "bell"))
            headers = {"Title": title, "Priority": prio, "Tags": tags}
            if dashboard_url:
                headers["Click"] = dashboard_url
            return headers, text.encode()
        if k == "slack":
            return {"Content-Type": "application/json"}, json.dumps({"text": f"*{title}*\n{text}"}).encode()
        if k == "discord":
            return {"Content-Type": "application/json"}, json.dumps({"content": f"**{title}**\n{text}"[:1990]}).encode()
        body = json.dumps({**ev, "title": title, "text": text, "folded_changes": folded}, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        if self.secret:
            ts = str(int(time.time()))
            headers.update({"X-VetoSentinel-Timestamp": ts, "X-VetoSentinel-Signature": sign(self.secret, ts, body)})
        return headers, body


class AlertDispatcher:
    def __init__(self, cfg: AlertsConfig, get_status: Callable[[], MonitorStatus], mode: str,
                 client: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.get_status = get_status
        self.mode = mode
        self.targets = [Target(w) for w in cfg.webhooks] if cfg.enabled else []
        self.active = [t for t in self.targets if t.url]
        self._client = client or httpx.Client(timeout=cfg.timeout_seconds, headers={"User-Agent": USER_AGENT})
        self._clock = clock
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state: str | None = None
        self._lock = threading.Lock()
        self.retry_delays = (1.0, 3.0)  # seconds before the 2nd and 3rd attempt

    def info(self) -> dict[str, Any]:
        return {"targets": [t.info() for t in self.targets], "active": len(self.active),
                "min_interval_seconds": self.cfg.min_interval_seconds}

    # ------------------------------------------------------------------ producer side (monitor listener)
    def notify(self, kind: str) -> None:
        """Monitor listener: queue an alert for a level change or an operator action."""
        if not self.active or kind not in ("STATE", "CONTROL"):
            return
        st = self.get_status()
        state = str(st.state)
        if kind == "STATE" and state == self._last_state:
            return
        prev, self._last_state = self._last_state, state
        self._q.put(event_from_status(kind, st, prev, self.mode))

    def test(self) -> list[dict[str, Any]]:
        """Send a clearly marked test alert to every configured target now (bypasses the rate limit)."""
        ev = {**event_from_status("STATE", self.get_status(), None, self.mode), "event": "test"}
        out = []
        for t in self.active:
            ok = self._send(t, ev, 0)
            out.append({**t.info(), "ok": ok})
        return out

    # ------------------------------------------------------------------ sender thread
    def start(self) -> None:
        if not self.active:
            return
        names = ", ".join(f"{t.cfg.kind} ({urlsplit(t.url).hostname})" for t in self.active)
        log.info("alerts on: %s", names)
        self._thread = threading.Thread(target=self._run, name="vs-alerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread:
            self._thread.join(self.cfg.timeout_seconds * 3 + 1)

    def pump(self, first: dict[str, Any] | None = None, now: float | None = None) -> None:
        """Move queued events onto targets and send whatever is due (the thread calls this)."""
        now = self._clock() if now is None else now
        events = [first] if first is not None else []
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            if ev is not None:
                events.append(ev)
        for ev in events:
            for t in self.active:
                if t.pending is not None:
                    t.folded += 1
                t.pending = ev
        for t in self.active:
            if t.pending is not None and now >= t.next_at:
                ev, folded = t.pending, t.folded
                t.pending, t.folded = None, 0
                self._send(t, ev, folded)
                t.next_at = now + self.cfg.min_interval_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._q.get(timeout=0.5)
            except queue.Empty:
                first = None
            try:
                self.pump(first)
            except Exception:  # noqa: BLE001 - alerts must never take anything down
                log.exception("alert dispatch failed")

    def _send(self, t: Target, ev: dict[str, Any], folded: int) -> bool:
        headers, body = t.request(ev, folded, self.cfg.dashboard_url)
        err = ""
        for attempt in range(3):
            try:
                r = self._client.post(t.url, content=body, headers=headers, timeout=self.cfg.timeout_seconds)
                if r.status_code < 300:
                    with self._lock:
                        t.sent += 1
                        t.last_result, t.last_at = f"HTTP {r.status_code}", iso_ms(utcnow())
                    return True
                err = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    break  # a client error will not fix itself
            except httpx.HTTPError as e:
                err = type(e).__name__
            if attempt < 2 and not self._stop.is_set():
                time.sleep(self.retry_delays[attempt])
        with self._lock:
            t.failed += 1
            t.last_result, t.last_at = f"failed: {err}", iso_ms(utcnow())
        log.warning("alert to %s (%s) failed: %s", t.cfg.kind, urlsplit(t.url).hostname, err)
        return False
