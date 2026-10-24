""""Optimize for": relevance scores, weighted statistics and the effect on the risk signals."""

import zlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from vetosentinel.config import (
    DriftConfig,
    FocusConfig,
    IntensityConfig,
    NormalizerConfig,
    PanicConfig,
    Settings,
)
from vetosentinel.features.backends import HashingEmbedder, LexiconSentiment
from vetosentinel.features.drift import DriftTracker
from vetosentinel.features.intensity import IntensityTracker
from vetosentinel.features.panic import PanicAggregator
from vetosentinel.features.pipeline import FeaturePipeline
from vetosentinel.features.relevance import RelevanceModel, parse_focus
from vetosentinel.models import DataOrigin, Headline, TimestampQuality

T0 = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)


def test_parse_focus_splits_dedupes_and_limits():
    assert parse_focus(" OPEC,  crude   oil;oil tankers\nOPEC ,, ") == ["OPEC", "crude oil", "oil tankers"]
    assert parse_focus("") == [] and len(parse_focus(",".join(f"w{i}" for i in range(40)))) == 12


def test_keyword_fallback_matches_word_stems_not_fragments():
    m = RelevanceModel("banks, crude oil prices", FocusConfig(), HashingEmbedder(64))
    assert m.is_fallback and m.calibrated
    assert m.keyword_relevance("Banking stocks slide") == 1.0  # bank ~ banking
    assert m.keyword_relevance("Regional banks under pressure") == 1.0
    assert m.keyword_relevance("Export ban on rice") == 0.0  # "ban" is not "banks"
    assert m.keyword_relevance("Oil prices jump") == pytest.approx(2 / 3)  # 2 of 3 words of a phrase
    with pytest.raises(ValueError):
        RelevanceModel(" , ;", FocusConfig(), HashingEmbedder(64))


class TopicEmbedder:
    """Deterministic fake 'semantic' embedder: texts mentioning oil point along axis 0."""

    is_fallback, implementation, dim = False, "fake-topics", 64

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.random.default_rng(zlib.crc32(t.encode())).normal(size=self.dim)
            if "oil" in t.lower():
                v[0] += 12.0
            out.append(v / np.linalg.norm(v))
        return np.asarray(out, dtype=np.float32)


def test_semantic_relevance_is_calibrated_on_past_headlines():
    emb = TopicEmbedder()
    texts = [f"oil story {i}" if i % 40 == 0 else f"general story {i}" for i in range(400)]
    m = RelevanceModel("oil", FocusConfig(calibration_min=300), emb)
    assert not m.calibrated and not m.calibrate(emb.embed(texts[:100]))  # too few to calibrate
    assert m.calibrate(emb.embed(texts))
    vecs = emb.embed(texts)
    r = m.relevance(texts, [vecs[i] for i in range(len(texts))])
    oil = np.array(["oil" in t for t in texts])
    assert r[oil].min() > 0.9 and np.median(r[~oil]) < 0.05
    # r = 0.5 exactly at z = z_mid; w = b + (1 - b) r
    assert m.relevance(["x"], [None])[0] == 0.0  # no vector -> off-focus
    assert m.weights(np.array([0.0, 1.0])).tolist() == [0.05, 1.0]
    eff = m.effect(r)
    assert eff["share"] == pytest.approx(r.mean()) and eff["focus_multiplier"] > 2
    assert eff["other_multiplier"] == pytest.approx(0.05 * eff["focus_multiplier"])


def test_weighted_drift_bias_correction_is_unbiased():
    """Two windows from the same topic mix: corrected D averages ~0 with or without weights,
    while the uncorrected estimate is biased upwards (small windows look 'far')."""
    rng = np.random.default_rng(1)
    mu = np.zeros(32)
    mu[0] = 1.0

    def mean_drift(correct, weighted):
        out = []
        for _ in range(60):
            tr = DriftTracker(DriftConfig(min_recent=3, min_reference=20, bias_correction=correct), 32)
            items = []
            for k in range(400):
                t = T0 + timedelta(minutes=10 * k)
                v = 0.6 * mu + rng.normal(size=32) / np.sqrt(32)
                w = float(rng.uniform(0.05, 1.0)) if weighted else 1.0
                items.append((t, v, w, f"h{k}"))
            tr.add_many(items)
            out.append(tr.compute(items[-1][0]).value)
        return float(np.mean(out))

    assert abs(mean_drift(True, True)) < 0.02 and abs(mean_drift(True, False)) < 0.02
    assert mean_drift(False, True) > 0.1


def test_weighted_panic_and_effective_count():
    p = PanicAggregator(PanicConfig(window_minutes=60, min_headlines=3))
    t = T0 + timedelta(minutes=30)
    for i, (pn, rel) in enumerate([(0.9, 1.0), (0.1, 0.05), (0.1, 0.05), (0.1, 0.05)]):
        assert p.add(t, pn, 1.0, f"h{i}", rel)
    r = p.compute(T0 + timedelta(minutes=59))
    assert r.value is None and "effective" in r.reason  # n_eff = 1.15^2 / 1.0075 ~ 1.3 < 3
    p.reweight(lambda _: 1.0)
    r = p.compute(T0 + timedelta(minutes=59))
    assert r.value == pytest.approx(0.3) and r.count == 4


def test_intensity_counts_relevance_weighted_headlines():
    it = IntensityTracker(IntensityConfig(), NormalizerConfig())
    for k in range(10):
        it.add(T0 + timedelta(minutes=k), 4.0, 1.0 if k < 2 else 0.05, f"h{k}")
    assert it.count(T0 - timedelta(minutes=1), T0 + timedelta(hours=1)) == pytest.approx(4 * (2 + 8 * 0.05))
    it.reweight(lambda _: 1.0)
    assert it.count(T0 - timedelta(minutes=1), T0 + timedelta(hours=1)) == 40


# ---------------------------------------------------------------- end to end on the pipeline
WORDS = ["retail", "football", "weather", "election", "film", "tech", "travel", "housing", "music"]


def hl(i, t, text):
    return Headline(headline_id=f"h{i}", headline=text, source="test", text_fingerprint=f"f{i}",
                    published_at=t, fetched_at=t, event_time=t, timestamp_quality=TimestampQuality.PROVIDER,
                    provider="test", data_origin=DataOrigin.SYNTHETIC)


def run(shock: str | None, focus: str, then_clear: bool = False):
    """6 days of news every 10 min: 6 assorted headlines + one oil headline per 20 min. In the last
    two hours, 3 headlines an hour about ``shock`` ("oil" or "football") turn very negative."""
    s = Settings()
    s.intensity.split_weekends, s.intensity.min_history_days = False, 3
    pipe = FeaturePipeline(s, LexiconSentiment(), HashingEmbedder(256))
    pipe.intensity.set_coverage_start(T0)
    rng = np.random.default_rng(7)
    n, t, end = 0, T0, T0 + timedelta(days=6)
    while t <= end:
        hot = shock is not None and t >= end - timedelta(hours=2)
        batch = []
        for _ in range(6):
            mood = rng.choice(["", " amid concerns", " as gains continue"])
            topic = rng.choice(WORDS)
            text = (f"{topic} crisis deepens as fears of collapse spread" if hot and shock == topic and n % 2
                    else f"{topic} news update {n}{mood}")
            batch.append(hl(n, t, text))
            n += 1
        if t.minute % 20 == 0:
            mood = rng.choice(["", " amid concerns", " as gains continue"])
            text = ("oil crisis: crude collapse, panic and fears of default" if hot and shock == "oil"
                    else f"oil output update {n}{mood}")
            batch.append(hl(n, t, text))
            n += 1
        pipe.ingest(batch)
        pipe.compute(t)  # like the monitor: take in the news up to t, then assess at t
        t += timedelta(minutes=10)
    if not focus:
        return pipe.compute(end)
    pipe.set_focus(focus, 0.05, rebuild_until=end)
    if then_clear:
        pipe.set_focus("", rebuild_until=end)
    return pipe.compute(end)


def test_focus_amplifies_relevant_news_and_damps_the_rest():
    """Effect of a shock = z with it minus z of the same run (same random draws) without it.
    The focused panic score rests on fewer effective headlines, so it is noisier; its baseline
    spread is wider to match, which is why the comparison is on calibrated z-scores."""
    focus = "oil, crude, refineries"

    def effect(shock, f):
        z = lambda snap: snap.components["panic"].z_unclipped  # noqa: E731
        return z(run(shock, f)) - z(run(None, f))

    oil_whole, oil_focus = effect("oil", ""), effect("oil", focus)
    fb_whole, fb_focus = effect("football", ""), effect("football", focus)
    assert oil_focus > 2.5 * oil_whole > 0  # the oil shock stands out much more
    assert 0 < fb_focus < fb_whole / 3  # an off-focus shock is damped, not ignored
    snap = run("oil", focus)
    assert snap.monitor_ready and snap.components["volume"].available


def test_back_to_whole_market_restores_the_unfocused_readings():
    never, back = run("oil", ""), run("oil", "oil, crude", then_clear=True)
    for k in ("panic", "drift", "volume"):
        assert back.components[k].raw == pytest.approx(never.components[k].raw, rel=1e-9)
        # baselines are rebuilt on a 10-minute grid, so they may differ by a sample at the edge
        assert back.components[k].z == pytest.approx(never.components[k].z, abs=0.05)
