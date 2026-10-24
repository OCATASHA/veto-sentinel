"""Command-line entry point: ``vetosentinel <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime

from vetosentinel.config import Settings, load_settings
from vetosentinel.timeutil import parse_iso_utc

log = logging.getLogger("vetosentinel")


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(getattr(args, "config", None))
    if getattr(args, "fallback_models", False):
        s.panic.backend = "lexicon"
        s.drift.backend = "hashing"
    if getattr(args, "pub", None):
        s.bus.pub_endpoint = args.pub
    if getattr(args, "port", None):
        s.service.dashboard_port = args.port
    if getattr(args, "host", None):
        s.service.dashboard_host = args.host
    return s


def _serve(rt, settings: Settings, no_dashboard: bool) -> None:
    # The handler only flips a flag: taking a lock here (e.g. Event.set) can deadlock if the signal
    # lands while the main thread already holds that lock inside Event.wait.
    stop = {"requested": False}

    def handle(*_):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    try:
        if no_dashboard:
            while not stop["requested"]:
                time.sleep(0.2)
        else:
            import uvicorn

            from vetosentinel.dashboard.app import create_app

            host, port = settings.service.dashboard_host, settings.service.dashboard_port
            log.info("dashboard: http://%s:%d", host, port)
            config = uvicorn.Config(create_app(rt), host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            while not stop["requested"]:
                time.sleep(0.2)
            server.should_exit = True
            t.join(5)
    finally:
        rt.shutdown()


def cmd_replay(args: argparse.Namespace) -> None:
    from vetosentinel.runtime import Runtime
    from vetosentinel.service import Backends, ReplayController
    from vetosentinel.store import Store

    s = _settings(args)
    if args.speed:
        s.replay.speed = args.speed
    store = Store(args.db or s.service.db_path)
    ctrl = ReplayController(s, args.fixture, store, Backends(s))
    meta = ctrl.adapter.meta
    if meta.is_synthetic:
        log.warning("REPLAYING SYNTHETIC DEMO DATA (%s) - not observed history", args.fixture)
    if args.fast_forward_days or s.replay.fast_forward_days:
        from datetime import timedelta
        days = args.fast_forward_days or s.replay.fast_forward_days
        n = ctrl.seek(ctrl.start + timedelta(days=days))
        log.info("fast-forwarded %d ticks to %s", n, ctrl.clock.now().isoformat())
    if args.seek:
        ctrl.seek(parse_iso_utc(args.seek))
    rt = Runtime(s, store, ctrl)
    rt.start(with_publisher=True, with_demo_client=not args.no_demo_client)
    if not args.paused:
        ctrl.play()
    log.info("replay run %s; publishing on %s", ctrl.monitor.run_id, s.bus.pub_endpoint)
    _serve(rt, s, args.no_dashboard)


def cmd_live(args: argparse.Namespace) -> None:
    from vetosentinel.runtime import Runtime
    from vetosentinel.service import Backends, LiveController
    from vetosentinel.store import Store

    s = _settings(args)
    store = Store(args.db or s.service.db_path)
    ctrl = LiveController(s, store, Backends(s))
    if not ctrl.adapters:
        log.warning("no live adapters enabled: the monitor will stay NOT READY (HALT)")
    ctrl.prepare()
    rt = Runtime(s, store, ctrl)
    rt.start(with_publisher=True, with_demo_client=not args.no_demo_client)
    log.info("live run %s; publishing on %s", ctrl.monitor.run_id, s.bus.pub_endpoint)
    _serve(rt, s, args.no_dashboard)


def cmd_evaluate(args: argparse.Namespace) -> None:
    from vetosentinel.evaluation.report import evaluate, to_markdown

    s = _settings(args)
    rep = evaluate(s, args.fixture, args.out, db_path=args.db or ":memory:")
    print(to_markdown(rep))
    if args.out:
        print(f"\nreports written to {args.out}/", file=sys.stderr)


def cmd_client_demo(args: argparse.Namespace) -> None:
    from vetosentinel.client import ClientConfig, StrategyHooks, VetoSentinelClient
    from vetosentinel.models import RiskState

    class PrintHooks(StrategyHooks):
        def on_state_change(self, old: RiskState, new: RiskState, reason: str) -> None:
            print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] LOCAL STATE {old} -> {new}: {reason[:140]}",
                  flush=True)

        def on_caution(self, fraction: float, reason: str) -> None:
            print(f"    hook: CAUTION -> position limit x{fraction}, no new openings (simulated)", flush=True)

        def on_halt(self, reason: str) -> None:
            print("    hook: HALT -> request cancellation of resting orders (simulated; no broker)", flush=True)

        def on_flatten(self, reason: str) -> None:
            print("    hook: FLATTEN instruction -> page a human; NOT liquidating automatically", flush=True)

    c = VetoSentinelClient(args.endpoint, args.topic, ClientConfig(heartbeat_timeout_ms=args.timeout_ms),
                          PrintHooks())
    c.start()
    print(f"Python client subscribed to {args.endpoint}; startup state = {c.current_state} (fail-safe)")
    try:
        t_end = time.monotonic() + args.duration if args.duration else None
        while t_end is None or time.monotonic() < t_end:
            # A strategy's hot path: local reads only, no network.
            t0 = time.perf_counter_ns()
            ok_open = c.can_submit_order(opening=True)
            dt = time.perf_counter_ns() - t0
            st = c.status()
            print(f"  state={c.current_state:8s} can_open={ok_open!s:5s} can_reduce={c.can_submit_order(False)!s:5s} "
                  f"limit={c.position_limit(100):6.1f} hb_age={st['heartbeat_age_s']} fault={st['fault']} "
                  f"check={dt}ns", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        c.stop()
        print(json.dumps(c.status()["stats"], indent=1))


def cmd_probe(args: argparse.Namespace) -> None:
    from vetosentinel.service import build_live_adapters

    s = _settings(args)
    runners = build_live_adapters(s)
    if not runners:
        print("no live adapters enabled in config")
    for r in runners:
        t0 = time.monotonic()
        res = r.run(datetime.now(UTC))
        dt = time.monotonic() - t0
        status = "OK " if res.ok else "ERR"
        print(f"{status} {r.adapter.name:12s} {dt:5.1f}s items={len(res.items):4d} malformed={res.malformed} "
              f"{res.error[:160]}")
        for it in res.items[:3]:
            print(f"      - {it.published_raw} | {(it.headline or '')[:100]}")


def cmd_fetch_gkg(args: argparse.Namespace) -> None:
    from vetosentinel.evaluation.gdelt_gkg import DEFAULT_THEMES, build

    events = []
    for e in args.event or []:
        name, _, span = e.partition("=")
        a, _, b = span.partition("..")
        events.append({"name": name, "kind": "stress", "onset": parse_iso_utc(a).isoformat(),
                       "end": parse_iso_utc(b).isoformat(), "label_source": "user-supplied"})
    meta = build(parse_iso_utc(args.start), parse_iso_utc(args.end), args.out, args.every_n,
                 tuple(args.themes.split(",")) if args.themes else DEFAULT_THEMES, events)
    print(json.dumps(meta, indent=1))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vetosentinel", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="TOML config file (default: built-in defaults)")
        p.add_argument("--fallback-models", action="store_true",
                       help="force the labelled fallback heuristics (no FinBERT/MiniLM)")
        p.add_argument("--db", help="DuckDB path (overrides config)")

    p = sub.add_parser("replay", help="replay a fixture with ZeroMQ publisher + dashboard")
    common(p)
    p.add_argument("--fixture", default="fixtures/synthetic_demo.jsonl")
    p.add_argument("--speed", type=float, help="simulated seconds per wall second")
    p.add_argument("--fast-forward-days", type=float, default=0.0)
    p.add_argument("--seek", help="fast-forward to this UTC time before playing")
    p.add_argument("--paused", action="store_true")
    p.add_argument("--pub", help="ZeroMQ PUB bind endpoint, e.g. tcp://*:5556")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--no-demo-client", action="store_true")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("live", help="run against live adapters from the config")
    common(p)
    p.add_argument("--pub")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--no-demo-client", action="store_true")
    p.set_defaults(fn=cmd_live)

    p = sub.add_parser("evaluate", help="headless replay + reproducible report")
    common(p)
    p.add_argument("--fixture", default="fixtures/synthetic_demo.jsonl")
    p.add_argument("--out", default="reports")
    p.set_defaults(fn=cmd_evaluate)

    p = sub.add_parser("client-demo", help="Python SDK demo strategy (prints local state)")
    p.add_argument("--endpoint", default="tcp://127.0.0.1:5556")
    p.add_argument("--topic", default="vs.v1")
    p.add_argument("--timeout-ms", type=int, default=3000)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--duration", type=float, default=0.0, help="seconds (0 = forever)")
    p.set_defaults(fn=cmd_client_demo)

    p = sub.add_parser("probe-adapters", help="one-shot fetch from each configured live adapter")
    common(p)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("fetch-gdelt-gkg", help="build a historical fixture from GDELT GKG raw files")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--every-n", type=int, default=4)
    p.add_argument("--themes", help="comma-separated GKG theme prefixes")
    p.add_argument("--event", action="append",
                   help="user-supplied scenario label: name=START..END (UTC ISO). Not invented by the tool.")
    p.set_defaults(fn=cmd_fetch_gkg)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "filelock", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args.fn(args)


if __name__ == "__main__":
    main()
