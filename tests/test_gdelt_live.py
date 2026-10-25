"""Live GDELT raw-file adapter, weighted news volume, model cache and the live warm start."""

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np

from vetosentinel.config import IntensityConfig, NormalizerConfig, Settings
from vetosentinel.engine.monitor import Monitor
from vetosentinel.features.backends import HashingEmbedder, LexiconSentiment
from vetosentinel.features.cache import CachedEmbedder, CachedSentiment, ModelCache
from vetosentinel.features.intensity import IntensityTracker
from vetosentinel.features.pipeline import FeaturePipeline
from vetosentinel.ingest.base import AdapterRunner
from vetosentinel.ingest.gdelt_gkg_live import GdeltGkgLiveAdapter
from vetosentinel.store import Store
from vetosentinel.timeutil import SimClock

NEWEST = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)  # a Wednesday


def gkg_zip(ts: datetime, n: int = 3) -> bytes:
    rows = []
    for i in range(n):
        r = [""] * 27
        r[1], r[3], r[4], r[7] = ts.strftime("%Y%m%d%H%M%S"), "ex.com", f"https://ex.com/{ts:%H%M}/{i}", "ECON_STOCKMARKET"
        r[26] = f"<PAGE_TITLE>Markets story number {i} at {ts:%Y-%m-%d %H:%M} about shares</PAGE_TITLE>"
        rows.append("\t".join(r))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", "\n".join(rows))
    return buf.getvalue()


class FakeGdelt:
    def __init__(self, newest=NEWEST, missing=()):
        self.newest, self.missing, self.calls, self.corrupt = newest, set(missing), [], False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url.endswith("lastupdate.txt"):
            data = gkg_zip(self.newest)
            md5 = hashlib.md5(b"tampered" if self.corrupt else data).hexdigest()
            stamp = self.newest.strftime("%Y%m%d%H%M%S")
            return httpx.Response(200, text=f"1 abc http://x/{stamp}.export.CSV.zip\n{len(data)} {md5} "
                                            f"http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip\n")
        stamp = url.rsplit("/", 1)[-1].split(".", 1)[0]
        ts = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        if ts in self.missing or ts > self.newest:
            return httpx.Response(404)
        return httpx.Response(200, content=gkg_zip(ts))


def adapter(tmp_path, fake, **kw):
    return GdeltGkgLiveAdapter(cache_dir=tmp_path / "cache", client=httpx.Client(transport=httpx.MockTransport(fake.handler)), **kw)


def test_fetch_latest_then_catch_up_and_cache(tmp_path):
    fake = FakeGdelt()
    a = adapter(tmp_path, fake)
    r = a.fetch(NEWEST)
    assert r.ok and len(r.items) == 3 and a.last_ts == NEWEST
    assert r.items[0].extra["count_weight"] == 1.0
    assert a.fetch(NEWEST).items == []  # nothing new
    fake.newest = NEWEST + timedelta(minutes=45)  # three new files appeared
    r = a.fetch(fake.newest)
    assert r.ok and len(r.items) == 9 and a.last_ts == fake.newest
    # downloads go to the storage bucket: the data.gdeltproject.org front end caches 404s for an hour
    assert fake.calls and all(u.startswith("https://storage.googleapis.com/data.gdeltproject.org/") for u in fake.calls)
    # a second adapter on the same cache reuses parsed files instead of downloading them
    b = adapter(tmp_path, fake)
    before = len(fake.calls)
    b.rows(NEWEST)
    assert len(fake.calls) == before


def recent_slot(back_minutes=15):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now.replace(minute=now.minute // 15 * 15) - timedelta(minutes=back_minutes)


def test_long_outage_becomes_a_gap_and_long_missing_files_are_given_up(tmp_path):
    fake = FakeGdelt(missing={NEWEST + timedelta(hours=3)})  # March: far past the late-file grace
    a = adapter(tmp_path, fake, max_catchup=4)
    a.fetch(NEWEST)
    fake.newest = NEWEST + timedelta(hours=3, minutes=15)
    r = a.fetch(fake.newest)
    assert r.ok and len(r.items) == 3 * 3  # last 4 files, one of them never published
    assert len(r.gaps) == 2  # the stretch beyond max_catchup + the given-up file
    assert not a.pending and a.data_through == fake.newest


def test_late_files_are_kept_pending_and_ingested_out_of_order(tmp_path):
    t0 = recent_slot(45)
    fake = FakeGdelt(newest=t0)
    a = adapter(tmp_path, fake)
    a.fetch(t0)
    fake.newest = t0 + timedelta(minutes=30)
    fake.missing = {t0 + timedelta(minutes=15)}  # announced earlier but still not downloadable
    r = a.fetch(fake.newest)
    assert len(r.items) == 3 and r.gaps == []  # the later file is used straight away
    assert a.pending == {t0 + timedelta(minutes=15)} and a.data_through == t0  # complete only up to t0
    fake.missing = set()
    r = a.fetch(fake.newest)
    assert len(r.items) == 3 and not a.pending and a.data_through == fake.newest


def test_brand_new_file_not_yet_downloadable_is_retried(tmp_path):
    recent = recent_slot()
    fake = FakeGdelt(newest=recent)
    a = adapter(tmp_path, fake)
    a.fetch(recent)
    fake.newest = recent + timedelta(minutes=15)
    fake.missing = {fake.newest}  # announced but still 404
    r = a.fetch(fake.newest)
    assert r.ok and r.items == [] and a.pending == {fake.newest} and a.data_through == recent
    fake.missing = set()
    r = a.fetch(fake.newest)
    assert len(r.items) == 3 and a.data_through == fake.newest


def test_checksum_mismatch_is_refused(tmp_path):
    fake = FakeGdelt()
    fake.corrupt = True
    r = adapter(tmp_path, fake).fetch(NEWEST)
    assert not r.ok and "checksum" in r.error


def test_unreachable_gdelt_is_a_failure_not_a_crash(tmp_path):
    def boom(request):
        raise httpx.ConnectError("offline")
    a = GdeltGkgLiveAdapter(cache_dir=tmp_path, client=httpx.Client(transport=httpx.MockTransport(boom)))
    r = a.fetch(NEWEST)
    assert not r.ok and "ConnectError" in r.error


def test_backfill_is_hourly_weighted_with_coverage_and_gaps(tmp_path):
    fake = FakeGdelt(missing={NEWEST - timedelta(hours=5)})
    a = adapter(tmp_path, fake, backfill_days=1)
    seen = []
    r = a.backfill(NEWEST, lambda d, t: seen.append((d, t)))
    assert r.ok and r.coverage_start == NEWEST - timedelta(days=1)
    # 23 hourly files up to 10:00 (the missing one replaced by a file from the same hour),
    # then the last two hours file by file
    hourly = [i for i in r.items if i.extra["count_weight"] == 4.0]
    assert len(hourly) == 3 * 23 and len(r.items) == 3 * 23 + 3 * 8
    assert {i.extra["count_weight"] for i in r.items} == {4.0, 1.0}
    assert r.gaps == [] and seen[-1] == (31, 31)
    whole_hour_missing = FakeGdelt(missing={NEWEST - timedelta(hours=2, minutes=15 * k) for k in range(4)})
    r2 = adapter(tmp_path / "b", whole_hour_missing, backfill_days=1).backfill(NEWEST)
    assert len(r2.gaps) == 1 and len(r2.items) == 3 * 22 + 3 * 8
    assert a.last_ts == a.data_through == NEWEST and not a.pending  # live polling continues from here


def test_backfill_leaves_a_newest_file_that_is_not_uploaded_yet_pending(tmp_path):
    top = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # announced on the hour...
    fake = FakeGdelt(newest=top, missing={top})  # ...but not downloadable yet
    a = adapter(tmp_path, fake, backfill_days=1)
    r = a.backfill(top)
    stamp = top.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert r.ok and not any(i.published_raw == stamp for i in r.items)
    assert a.pending == {top} and a.data_through == top - timedelta(minutes=15)  # not complete yet
    fake.missing = set()
    r = a.fetch(top)  # it has been uploaded: the first live poll collects it instead of skipping it
    assert len(r.items) == 3 and {i.published_raw for i in r.items} == {stamp}
    assert not a.pending and a.data_through == top


def test_backfill_counts_news_volume_once_around_the_live_start(tmp_path):
    newest = NEWEST + timedelta(minutes=30)
    a = adapter(tmp_path, FakeGdelt(newest=newest), backfill_days=2)
    items = a.backfill(newest).items + a.fetch(newest).items  # the warm start, then the first live poll
    it = IntensityTracker(IntensityConfig(), NormalizerConfig())
    for i in items:
        assert i.published_raw is not None
        it.add(datetime.fromisoformat(i.published_raw), i.extra["count_weight"])
    hour = timedelta(hours=1)
    # every 60-minute window the first live assessments use holds exactly four files' worth
    # (3 headlines each), with no hourly sample counted on top of the 15-minute files
    for back in (45, 30, 15, 0):
        t = newest - timedelta(minutes=back)
        assert it.count(t - hour, t) == 12, t
    assert it.count(newest - 6 * hour, newest - 5 * hour) == 12  # hourly samples: 3 headlines x 4


def test_weighted_volume_counts_match_live_counts():
    it = IntensityTracker(IntensityConfig(), NormalizerConfig())
    t = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
    for _k in range(10):  # one hourly sample of 10 headlines, weight 4
        it.add(t, 4.0)
    for m in (15, 30, 45, 60):  # four live files of 10 headlines, weight 1
        for _k in range(10):
            it.add(t + timedelta(hours=1, minutes=m), 1.0)
    assert it.count(t - timedelta(hours=1), t) == 40
    assert it.count(t + timedelta(hours=1), t + timedelta(hours=2)) == 40


class CountingSentiment(LexiconSentiment):
    calls = 0

    def negative_probs(self, texts):
        CountingSentiment.calls += len(texts)
        return super().negative_probs(texts)


def test_model_cache_avoids_rescoring(tmp_path):
    cache = ModelCache(tmp_path / "m.sqlite")
    s = CachedSentiment(CountingSentiment(), cache)
    s.implementation = "test-model"
    texts = ["Banks tumble on panic", "Calm markets", "Banks tumble on panic"]
    first = s.negative_probs(texts)
    assert CountingSentiment.calls == 2  # duplicates scored once
    assert s.negative_probs(texts) == first and CountingSentiment.calls == 2
    e = CachedEmbedder(HashingEmbedder(32), ModelCache(tmp_path / "m.sqlite"))
    v1, v2 = e.embed(texts), e.embed(texts)
    assert np.allclose(v1, v2) and v1.shape == (3, 32)


def test_live_warm_start_makes_monitor_ready_immediately(tmp_path):
    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    fake = FakeGdelt(newest=NEWEST)
    a = adapter(tmp_path, fake, backfill_days=8)
    clock = SimClock(NEWEST + timedelta(minutes=5))
    mon = Monitor(s, Store(":memory:"), FeaturePipeline(s, LexiconSentiment(), HashingEmbedder(64)),
                  [AdapterRunner(a, poll_seconds=60)], clock, mode="live", data_origin="live_provider")
    res = a.backfill(clock.now())
    mon.ingest_result(mon.adapters[0], res, clock.now())
    mon.warm_up(clock.now() - timedelta(days=7), clock.now())
    d = mon.tick()
    assert d.health["monitor_ready"], d.health["not_ready_reasons"]


class LaggingSource:
    name, data_origin = "lagging", "live_provider"

    def __init__(self, through):
        self.data_through = through

    def fetch(self, now):
        from vetosentinel.ingest.base import FetchResult
        return FetchResult(ok=True)


def test_monitor_assesses_at_complete_data_time_and_flags_big_lags():
    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    src = LaggingSource(NEWEST - timedelta(minutes=45))
    clock = SimClock(NEWEST)
    mon = Monitor(s, Store(":memory:"), FeaturePipeline(s, LexiconSentiment(), HashingEmbedder(64)),
                  [AdapterRunner(src, poll_seconds=60)], clock, mode="live", data_origin="live_provider")
    d = mon.tick()
    assert d.as_of == NEWEST - timedelta(minutes=45)  # assessed as of complete data, not wall clock
    assert not any("behind" in r for r in d.health["not_ready_reasons"])
    clock.advance(timedelta(hours=3))  # the source stops advancing
    d = mon.tick()
    assert any("min behind" in r for r in d.health["not_ready_reasons"]) and d.new_state.value == "HALT"
    src.data_through = NEWEST - timedelta(hours=5)  # a source must never move assessment backwards
    assert mon.eval_time(clock.now()) == NEWEST - timedelta(minutes=45)


def test_a_skipped_file_stops_freezing_the_clock_once_newer_news_arrives(tmp_path):
    t0 = recent_slot(120)
    fake = FakeGdelt(newest=t0)
    a = adapter(tmp_path, fake)
    a.fetch(t0)
    gap_file = t0 + timedelta(minutes=15)
    fake.missing = {gap_file}
    fake.newest = t0 + timedelta(minutes=45)
    a.fetch(fake.newest)
    assert a.data_through == t0  # still waiting: newer files are less than an hour newer
    fake.newest = t0 + timedelta(minutes=75)
    a.fetch(fake.newest)
    assert a.data_through == fake.newest  # an hour of newer news arrived: stop waiting
    assert gap_file in a.late
    fake.missing = set()  # the late file finally appears: still collected
    r = a.fetch(fake.newest)
    assert len(r.items) == 3 and not a.late


def test_live_calm_countdown_is_real_time_and_waits_for_newer_news(tmp_path):
    from types import SimpleNamespace

    from vetosentinel.dashboard.app import cooldown_json

    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    fake = FakeGdelt(newest=NEWEST)
    a = adapter(tmp_path, fake, backfill_days=8)
    clock = SimClock(NEWEST + timedelta(minutes=5))
    mon = Monitor(s, Store(":memory:"), FeaturePipeline(s, LexiconSentiment(), HashingEmbedder(64)),
                  [AdapterRunner(a, poll_seconds=60)], clock, mode="live", data_origin="live_provider")
    mon.ingest_result(mon.adapters[0], a.backfill(clock.now()), clock.now())
    mon.warm_up(clock.now() - timedelta(days=7), clock.now())
    d = mon.tick()
    assert d.rule == "cooldown_pending", d.reason  # calm news: the startup HALT starts cooling off
    rt = SimpleNamespace(controller=SimpleNamespace(monitor=mon))  # a live controller (no restart())
    clock.advance(timedelta(minutes=10))
    cd = cooldown_json(rt)  # exact on the real clock, even though no new GDELT file has arrived
    assert cd is not None and cd["remaining_now_s"] == cd["remaining_s"] == 1200 and cd["rate"] == 1
    clock.advance(timedelta(minutes=21))  # 31 minutes, still no newer file: don't treat silence as calm
    d = mon.tick()
    assert d.new_state.value == "HALT" and "waiting for newer news" in d.reason
    cd = cooldown_json(rt)
    assert cd is not None and cd["needs_newer_news"]
    fake.newest = NEWEST + timedelta(minutes=15)  # GDELT catches up with calm news
    clock.advance(timedelta(minutes=2))
    d = mon.tick()
    assert d.new_state.value == "CAUTION" and d.rule == "deescalate_after_cooldown"
