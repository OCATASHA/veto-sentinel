"""Demo preflight: check everything a demo needs, before the demo (``vetosentinel preflight``).

Each check prints OK, WARN (works, but not at its best) or FAIL (a demo path will break), with
the fix. Exit code 1 if anything FAILs. Network checks (GDELT, the Gemini key) are skipped with
``--offline``; ``--send-test-alert`` also sends a test message to every configured alert target.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from vetosentinel.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
SVB = ROOT / "data" / "historical" / "gdelt_gkg_svb_2023.jsonl"
SYNTHETIC = ROOT / "fixtures" / "synthetic_demo.jsonl"
CPP_BOT = ROOT / "clients" / "cpp" / "build" / "strategy_demo"
# (script, dashboard port, bus port): the run scripts' defaults; fleet reports use bus port + 100
SCRIPTS = (("run_dashboard.sh", 8000, 5556), ("run_history.sh", 8001, 5557), ("run_live.sh", 8002, 5558))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append((status, name, detail, fix))
        line = f"  {MARK[status]} {name}: {detail}"
        print(line)
        if fix and status != OK:
            print(f"      -> {fix}")

    def run(self, name: str, fn: Callable[[Report], None]) -> None:
        try:
            fn(self)
        except Exception as e:  # noqa: BLE001 - a broken check must not hide the others
            self.add(FAIL, name, f"check crashed: {type(e).__name__}: {e}")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r[0] == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for r in self.rows if r[0] == WARN)


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------- checks
def check_python(r: Report) -> None:
    mach = platform.machine()
    native = not (platform.system() == "Darwin" and mach == "x86_64" and "ARM64" in platform.version())
    r.add(OK if native else WARN, "Python", f"{platform.python_version()} on {mach}",
          "an x86_64 (Rosetta) Python can't use the torch wheels: use the uv-managed arm64 Python (.python-version)")


def check_models(r: Report, s: Settings) -> None:
    have = [m for m in ("transformers", "sentence_transformers", "torch") if importlib.util.find_spec(m)]
    if len(have) < 3:
        r.add(WARN, "AI models", "packages not installed: the dashboard will use the labelled fallback heuristics",
              "uv sync --extra ml")
        return
    from huggingface_hub import try_to_load_from_cache  # installed with transformers

    missing = [name for name in (s.panic.model_name, s.drift.model_name)
               if not isinstance(try_to_load_from_cache(name, "config.json"), str)]
    if missing:
        r.add(FAIL, "AI models", f"not downloaded yet: {', '.join(missing)} (the run scripts work offline only once cached)",
              "start ./run_dashboard.sh once with internet (it caches FinBERT and MiniLM, ~0.5 GB)")
    else:
        r.add(OK, "AI models", f"{s.panic.model_name} and {s.drift.model_name} are cached locally")
    cache = ROOT / (s.service.model_cache_path or "data/model_cache.sqlite")
    if cache.exists():
        with sqlite3.connect(cache) as con:
            n = con.execute("SELECT count(*) FROM sentiment").fetchone()[0]
        r.add(OK, "Model result cache", f"{n:,} scored headlines in {cache.relative_to(ROOT)} (replays start fast)")
    else:
        r.add(WARN, "Model result cache", "empty: the first replay scores every headline (a few minutes)",
              "run ./run_history.sh once before the demo")


def check_data(r: Report) -> None:
    for path, label, need in ((SYNTHETIC, "Demo data", True), (SVB, "SVB 2023 dataset", False)):
        if not path.exists():
            r.add(FAIL if need else WARN, label, f"missing: {path.relative_to(ROOT)}",
                  "git checkout the file" if need else "./run_history.sh --download")
            continue
        sha = _sha256(path)
        runs = []
        for rep in sorted((ROOT / "reports").glob("*/*_report.json")):
            try:
                if json.loads(rep.read_text())["fixture"]["sha256"] == sha:
                    runs.append(rep.parent.name)
            except (OSError, ValueError, KeyError):
                continue
        r.add(OK, label, f"{path.stat().st_size / 1e6:.1f} MB; evaluation runs for the comparison chart: "
              + (", ".join(runs) if runs else "none"))
    markers = SVB.with_suffix(".markers.json")
    r.add(OK if markers.exists() else WARN, "SVB event pins", "present" if markers.exists() else "missing",
          "git checkout data/historical/gdelt_gkg_svb_2023.markers.json")


def check_cpp(r: Report) -> None:
    if not CPP_BOT.exists():
        r.add(WARN, "C++ bot", "not built: the fleet view's 'Start a C++ bot' button stays hidden",
              "cmake -S clients/cpp -B clients/cpp/build -DCMAKE_BUILD_TYPE=Release && cmake --build clients/cpp/build -j")
        return
    out = subprocess.run([str(CPP_BOT), "tcp://127.0.0.1:1", "0.4"], capture_output=True, text=True, timeout=20).stdout
    m = re.search(r"SUMMARY (\{.*\})", out)
    if not m:
        r.add(FAIL, "C++ bot", "built but did not run cleanly", "rebuild: cmake --build clients/cpp/build -j")
        return
    summary = json.loads(m.group(1))
    r.add(OK, "C++ bot", f"runs; starts at {summary['local_state']} with no monitor (fail-safe), "
          f"order check {summary['check_mean_ns']:.0f} ns including the timer")


def check_ports(r: Report) -> None:
    for script, web, bus in SCRIPTS:
        busy = [p for p in (web, bus, bus + 100) if not _port_free(p)]
        if busy:
            r.add(WARN, f"Ports for {script}", f"in use: {', '.join(map(str, busy))} (already running? that's fine)",
                  f"{script} --port N --pub-port M for a second copy")
        else:
            r.add(OK, f"Ports for {script}", f"{web} (dashboard), {bus} (bus) and {bus + 100} (fleet) are free")


def check_disk(r: Report) -> None:
    free = shutil.disk_usage(ROOT).free / 1e9
    cache = ROOT / "data" / "gdelt_cache"
    n = len(list(cache.glob("*.jsonl"))) if cache.exists() else 0
    r.add(OK if free > 3 else WARN, "Disk", f"{free:.0f} GB free; GDELT cache holds {n:,} files",
          "the first live start downloads 1-2 GB")


def check_gdelt(r: Report) -> None:
    from vetosentinel.ingest.gdelt_gkg_live import LASTUPDATE

    t0 = time.perf_counter()
    try:
        resp = httpx.get(LASTUPDATE, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        r.add(WARN, "GDELT (live news)", f"unreachable ({type(e).__name__}): ./run_live.sh would not be ready",
              "check the network; ./run_history.sh and ./run_dashboard.sh work offline")
        return
    stamp = re.search(r"(\d{14})\.gkg\.csv\.zip", resp.text)
    age = ""
    if stamp:
        ts = datetime.strptime(stamp.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        mins = int((datetime.now(UTC) - ts).total_seconds() // 60)
        # GDELT announces a batch a few minutes before the time it is stamped with
        age = f", newest batch {mins} min old" if mins >= 0 else f", newest batch stamped {ts:%H:%M} UTC (announced early)"
    r.add(OK, "GDELT (live news)", f"reachable in {(time.perf_counter() - t0) * 1000:.0f} ms{age}")


def check_login(r: Report) -> None:
    if os.environ.get("VETOSENTINEL_LOGIN_PASSWORD"):
        r.add(OK, "Login", f"custom account '{os.environ.get('VETOSENTINEL_LOGIN_USER') or 'oxhack26'}' from the environment")
    else:
        r.add(OK, "Login", "built-in demo account (the team has the password)")
    if os.environ.get("VETOSENTINEL_OPERATOR_TOKEN"):
        r.add(WARN, "Operator token", "set: the dashboard's buttons become read-only",
              "unset VETOSENTINEL_OPERATOR_TOKEN for a hands-on demo")


def check_ai(r: Report, s: Settings, online: bool) -> None:
    key = os.environ.get(s.ai.api_key_env, "").strip()
    if not key:
        r.add(WARN, "AI summaries (Gemini)", f"off: {s.ai.api_key_env} not set (everything else works without it)",
              f"put {s.ai.api_key_env}=... in .env")
        return
    if not online:
        r.add(OK, "AI summaries (Gemini)", "key set (not checked: --offline)")
        return
    try:
        resp = httpx.get(f"{s.ai.endpoint}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=10)
    except httpx.HTTPError as e:
        r.add(WARN, "AI summaries (Gemini)", f"API unreachable ({type(e).__name__})", "check the network")
        return
    if resp.status_code != 200:
        r.add(FAIL, "AI summaries (Gemini)", f"key rejected (HTTP {resp.status_code})", "check the key in .env")
        return
    names = {m["name"].split("/", 1)[-1] for m in resp.json().get("models", [])}
    usable = [m for m in s.ai.models if m in names]
    r.add(OK if usable else FAIL, "AI summaries (Gemini)",
          f"key works; configured models available: {', '.join(usable) or 'none'}",
          "set ai.models in config to models this key can use")


def check_alerts(r: Report, s: Settings, send: bool) -> None:
    from vetosentinel.alerts import Target, event_test

    targets = [Target(w) for w in s.alerts.webhooks]
    active = [t for t in targets if t.url]
    if not active:
        names = ", ".join(t.cfg.url_env for t in targets if t.cfg.url_env)
        r.add(WARN, "Alerts", f"none configured (set one of {names} in .env for phone or chat alerts)")
        return
    if not send:
        r.add(OK, "Alerts", ", ".join(f"{t.cfg.kind} ({t.info()['host']})" for t in active)
              + " (send one with --send-test-alert)")
        return
    with httpx.Client(timeout=s.alerts.timeout_seconds) as client:
        for t in active:
            headers, body = t.request(event_test(), 0, s.alerts.dashboard_url)
            try:
                code = client.post(t.url, content=body, headers=headers).status_code
            except httpx.HTTPError as e:
                code = type(e).__name__
            ok = isinstance(code, int) and code < 300
            r.add(OK if ok else FAIL, f"Alert test ({t.cfg.kind})", f"{t.info()['host']}: {code}",
                  "check the URL in .env")


def run(config: str | None = None, online: bool = True, send_test_alert: bool = False) -> int:
    s = load_settings(config or (ROOT / "config" / "default.toml"))
    print("Veto Sentinel preflight\n")
    r = Report()
    r.run("Python", check_python)
    r.run("AI models", lambda rep: check_models(rep, s))
    r.run("Data", check_data)
    r.run("C++ bot", check_cpp)
    r.run("Ports", check_ports)
    r.run("Disk", check_disk)
    if online:
        r.run("GDELT", check_gdelt)
    r.run("Login", check_login)
    r.run("AI", lambda rep: check_ai(rep, s, online))
    r.run("Alerts", lambda rep: check_alerts(rep, s, send_test_alert))
    print(f"\n{len(r.rows) - r.failed - r.warned} OK, {r.warned} warning(s), {r.failed} failure(s)"
          + (": ready for a demo." if not r.failed else ": fix the failures above before the demo."))
    return 1 if r.failed else 0
