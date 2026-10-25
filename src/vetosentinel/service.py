"""Runtime assembly: backends, adapters, replay/live controllers, publisher, demo client."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from vetosentinel.config import Settings
from vetosentinel.engine.monitor import Monitor
from vetosentinel.features.backends import load_embedder, load_sentiment
from vetosentinel.features.cache import CachedEmbedder, CachedSentiment, ModelCache
from vetosentinel.features.pipeline import FeaturePipeline
from vetosentinel.ingest.base import AdapterRunner, FetchResult
from vetosentinel.ingest.fixture import FixtureAdapter, load_markers
from vetosentinel.ingest.gdelt import GdeltDocAdapter
from vetosentinel.ingest.gdelt_gkg_live import GdeltGkgLiveAdapter
from vetosentinel.ingest.gkg import DEFAULT_THEMES
from vetosentinel.ingest.newsapi import NewsApiAdapter
from vetosentinel.ingest.rss import RssAdapter
from vetosentinel.store import Store
from vetosentinel.timeutil import SimClock, SystemClock, parse_iso_utc

log = logging.getLogger(__name__)


class Backends:
    """Load models once; reused across replay restarts."""

    def __init__(self, settings: Settings):
        self.sentiment, n1 = load_sentiment(settings.panic.backend, settings.panic.model_name,
                                            settings.panic.batch_size)
        self.embedder, n2 = load_embedder(settings.drift.backend, settings.drift.model_name,
                                          settings.drift.hashing_dim)
        self.notes = [n1, n2]
        path = settings.service.model_cache_path
        if path and not (self.sentiment.is_fallback and self.embedder.is_fallback):
            cache = ModelCache(path)
            if not self.sentiment.is_fallback:
                self.sentiment = CachedSentiment(self.sentiment, cache)
            if not self.embedder.is_fallback:
                self.embedder = CachedEmbedder(self.embedder, cache)


class Liveness:
    def __init__(self) -> None:
        self.t = time.monotonic()

    def beat(self) -> None:
        self.t = time.monotonic()


def build_live_adapters(settings: Settings) -> list[AdapterRunner]:
    runners = []
    for a in settings.ingest.adapters:
        if not a.enabled:
            continue
        if a.kind == "gdelt":
            ad = GdeltDocAdapter(a.name, a.query, a.max_records, timeout=a.timeout_seconds)
        elif a.kind == "gdelt_gkg":
            ad = GdeltGkgLiveAdapter(a.name, tuple(a.themes) if a.themes else DEFAULT_THEMES, a.backfill_days,
                                     a.cache_dir, timeout=a.timeout_seconds)
        elif a.kind == "rss":
            if not a.url:
                raise ValueError(f"rss adapter {a.name} needs url")
            ad = RssAdapter(a.name, a.url, a.source, a.timeout_seconds)
        elif a.kind == "newsapi":
            ad = NewsApiAdapter(a.name, a.endpoint, a.api_key_env, a.params or None, a.timeout_seconds)
            if not ad.configured:
                log.warning("adapter %s disabled: %s not set", a.name, a.api_key_env)
                continue
        elif a.kind == "fixture":
            raise ValueError("fixture adapters are used via `vetosentinel replay`, not live mode")
        else:  # pragma: no cover - validated by config
            raise ValueError(a.kind)
        runners.append(AdapterRunner(ad, a.poll_seconds))
    return runners


class ReplayController:
    """Drives a Monitor over a fixture on a simulated clock with play/pause/speed/seek."""

    def __init__(self, settings: Settings, fixture_path: str, store: Store, backends: Backends,
                 liveness: Liveness | None = None):
        self.s = settings
        self.fixture_path = fixture_path
        self.store = store
        self.backends = backends
        self.liveness = liveness or Liveness()
        self.lock = threading.RLock()
        self.speed = settings.replay.speed
        self.last_step_mono = time.monotonic()
        self.playing = False
        self.finished = False
        self.tick = timedelta(minutes=settings.replay.tick_minutes)
        self.markers = load_markers(fixture_path)  # dashboard context only (see load_markers)
        self._new_monitor()

    def _new_monitor(self) -> None:
        self.adapter = FixtureAdapter(self.fixture_path)
        if self.adapter.start is None:
            raise ValueError("fixture has no usable items")
        start = self.adapter.start.replace(minute=0, second=0, microsecond=0)
        self.start, self.end = start, self.adapter.end
        self.clock = SimClock(start)
        pipeline = FeaturePipeline(self.s, self.backends.sentiment, self.backends.embedder)
        meta = self.adapter.meta
        self.monitor = Monitor(
            self.s, self.store, pipeline, [AdapterRunner(self.adapter, poll_seconds=1.0)], self.clock,
            mode="replay", data_origin=str(meta.data_origin), model_notes=self.backends.notes,
            coverage_start=start, fixture_meta=meta.raw, fixture_path=self.fixture_path)
        # Fixture-declared data gaps (e.g. provider files that failed to download) are
        # missing coverage, not quiet periods: exclude them from the intensity baseline.
        for g in meta.raw.get("gaps", []):
            pipeline.intensity.mark_gap(parse_iso_utc(g["start"]), parse_iso_utc(g["end"]))
        self.finished = False
        self.on_restart: list = getattr(self, "on_restart", [])
        for fn in self.on_restart:
            fn(self.monitor)

    # ------------------------------------------------------------------ controls
    def play(self) -> None:
        self.playing = True

    def pause(self) -> None:
        self.playing = False

    def set_speed(self, speed: float) -> None:
        if not (1.0 <= speed <= 1e6):
            raise ValueError("speed must be in [1, 1e6] simulated seconds per wall second")
        self.speed = speed

    def restart(self) -> None:
        with self.lock:
            self.store.end_run(self.monitor.run_id)
            self._new_monitor()

    def step(self) -> bool:
        """Advance one tick. Returns False when the fixture is exhausted."""
        with self.lock:
            if self.finished:
                return False
            if self.clock.now() + self.tick > self.end:
                self.finished = True
                self.playing = False
                self.store.end_run(self.monitor.run_id)
                return False
            self.clock.advance(self.tick)
            self.monitor.tick()
            self.last_step_mono = time.monotonic()
            self.liveness.beat()
            return True

    def seek(self, target: datetime) -> int:
        """Fast-forward (never backwards: that would break causality) to ``target``."""
        n = 0
        while self.clock.now() + self.tick <= target and self.step():
            n += 1
            if n % 50 == 0:
                self.liveness.beat()
        return n

    def run_to_end(self) -> None:
        while self.step():
            pass

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.liveness.beat()
            if not self.playing or self.finished:
                stop.wait(0.1)
                continue
            t0 = time.monotonic()
            self.step()
            # Wait out the rest of this tick in short slices (stays responsive to pause/stop and
            # to speed changes) instead of capping the wait, which made slow speeds run fast.
            while not stop.is_set() and self.playing:
                left = t0 + self.tick.total_seconds() / self.speed - time.monotonic()
                if left <= 0:
                    break
                self.liveness.beat()
                stop.wait(min(left, 0.2))

    def info(self) -> dict[str, Any]:
        now = self.clock.now()
        span = (self.end - self.start).total_seconds()
        meta = self.adapter.meta
        return {"mode": "replay", "sim_time": now.isoformat(), "start": self.start.isoformat(),
                "end": self.end.isoformat(), "progress": min(1.0, (now - self.start).total_seconds() / span),
                "playing": self.playing, "finished": self.finished, "speed": self.speed,
                "tick_minutes": self.tick.total_seconds() / 60, "fixture": self.fixture_path,
                "data_origin": str(meta.data_origin), "synthetic": meta.is_synthetic,
                "fixture_title": meta.title, "fixture_warning": meta.raw.get("warning", ""),
                "scenarios": meta.scenarios, "markers": self.markers}


class LiveController:
    def __init__(self, settings: Settings, store: Store, backends: Backends, liveness: Liveness | None = None):
        self.s = settings
        self.liveness = liveness or Liveness()
        pipeline = FeaturePipeline(settings, backends.sentiment, backends.embedder)
        self.adapters = build_live_adapters(settings)
        self.monitor = Monitor(settings, store, pipeline, self.adapters, SystemClock(), mode="live",
                               data_origin="live_provider", model_notes=backends.notes)
        self.on_restart: list = []

    def prepare(self, chunk: int = 4000) -> None:
        """Backfill recent history (adapters that support it) and warm the rolling baselines, so
        the monitor can judge from the first tick instead of after days. Runs before serving."""
        now = datetime.now(UTC)
        earliest = None
        for runner in self.adapters:
            backfill = getattr(runner.adapter, "backfill", None)
            if backfill is None or not getattr(runner.adapter, "backfill_days", 0):
                continue
            log.info("%s: loading the last %g days of news from the GDELT archive (cached after the first time)...",
                     runner.adapter.name, runner.adapter.backfill_days)
            shown = [0]

            def progress(done: int, total: int, name: str = runner.adapter.name, shown: list = shown) -> None:
                if done == total or done - shown[0] >= max(1, total // 10):
                    shown[0] = done
                    log.info("%s: archive files %d/%d", name, done, total)

            res = backfill(now, progress)
            if not res.ok:
                log.warning("%s: backfill failed (%s); starting without history", runner.adapter.name, res.error)
                continue
            items = res.items
            for i in range(0, max(1, len(items)), chunk):  # chunked so progress is visible while scoring
                part = FetchResult(ok=True, items=items[i:i + chunk],
                                   coverage_start=res.coverage_start, gaps=res.gaps if i == 0 else [])
                self.monitor.ingest_result(runner, part, now)
                log.info("%s: scored %d/%d archived headlines", runner.adapter.name, min(i + chunk, len(items)), len(items))
            if res.coverage_start and (earliest is None or res.coverage_start < earliest):
                earliest = res.coverage_start
        if earliest is not None:
            end = self.monitor.eval_time(now)
            start = max(earliest, end - timedelta(days=self.s.normalizer.baseline_days + 1))
            n = self.monitor.warm_up(start, end)
            log.info("warm-up: replayed %d baseline steps; the monitor can judge from the first tick", n)

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.liveness.beat()
            try:
                self.monitor.tick()
            except Exception:  # noqa: BLE001 - keep looping; publisher self-watchdog covers hangs
                log.exception("monitor tick failed")
            self.liveness.beat()
            deadline = time.monotonic() + self.s.service.tick_seconds
            cd = self.monitor.cooldown_status()
            if cd is not None and cd["remaining_s"] > 0:  # tick right when a calm period ends
                deadline = min(deadline, time.monotonic() + cd["remaining_s"] + 0.5)
            while not stop.is_set() and time.monotonic() < deadline:
                self.liveness.beat()
                stop.wait(0.5)

    def info(self) -> dict[str, Any]:
        return {"mode": "live", "sim_time": datetime.now(UTC).isoformat(), "data_origin": "live_provider",
                "synthetic": False, "playing": True, "finished": False,
                "tick_seconds": self.s.service.tick_seconds}
