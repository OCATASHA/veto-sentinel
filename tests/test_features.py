import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from vetosentinel.config import (
    CompositeConfig,
    CusumConfig,
    DriftConfig,
    IntensityConfig,
    NormalizerConfig,
    PanicConfig,
)
from vetosentinel.detect.cusum import CusumDetector
from vetosentinel.features.backends import HashingEmbedder, LexiconSentiment, resolve_negative_label
from vetosentinel.features.composite import composite
from vetosentinel.features.drift import DriftTracker
from vetosentinel.features.intensity import IntensityTracker
from vetosentinel.features.normalize import RollingNormalizer, robust_z
from vetosentinel.features.panic import PanicAggregator
from vetosentinel.models import ComponentReading

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # a Monday


# ---------------------------------------------------------------- normaliser
def test_robust_z_zero_variance_uses_floor():
    z, z_raw, c, s = robust_z(1.0, np.zeros(50), "robust", 0.1, 10, 0.0)
    assert s == 0.1 and z == 10.0 and z_raw == pytest.approx(10.0)


def test_robust_z_clip_and_floor():
    base = np.random.default_rng(0).normal(0, 1, 500)
    z, *_ = robust_z(-50, base, "robust", 0.01, 10, 0.0)
    assert z == 0.0
    z, *_ = robust_z(-50, base, "standard", 0.01, 10, -3.0)
    assert z == -3.0
    z, *_ = robust_z(1e9, base, "robust", 0.01, 10, 0.0)
    assert z == 10.0


def test_robust_baseline_resists_outliers():
    base = np.concatenate([np.full(95, 0.3), np.full(5, 0.99)])
    z_rob, *_ = robust_z(0.5, base, "robust", 0.02, 10, 0)
    z_std, *_ = robust_z(0.5, base, "standard", 0.02, 10, 0)
    assert z_rob > z_std


def test_rolling_normalizer_warmup_and_gap():
    cfg = NormalizerConfig(min_baseline_samples=10, baseline_gap_minutes=60)
    n = RollingNormalizer("panic", cfg)
    for i in range(10):
        z, _, info = n.score(T0 + timedelta(minutes=5 * i), 0.3)
        assert z is None and "warm-up" in info.reason
        n.observe(T0 + timedelta(minutes=5 * i), 0.3)
    # all 10 samples are within the last hour -> excluded by the gap
    z, _, info = n.score(T0 + timedelta(minutes=50), 0.3)
    assert z is None and info.baseline_count == 0
    z, _, info = n.score(T0 + timedelta(minutes=50 + 60), 0.3)
    assert info.ready and z == 0.0


def test_rolling_normalizer_rejects_nonfinite():
    n = RollingNormalizer("panic", NormalizerConfig(min_baseline_samples=3))
    n.observe(T0, float("nan"))
    n.observe(T0, float("inf"))
    assert n.baseline(T0 + timedelta(days=1)).size == 0
    z, _, info = n.score(T0, float("nan"))
    assert z is None


# ---------------------------------------------------------------- composite
def reading(name, z, available=True):
    return ComponentReading(name=name, available=available, z=z if available else None,
                            reason="" if available else "warm-up")


def test_composite_weighted_sum():
    r, deg, nr = composite({"panic": reading("panic", 2.0), "drift": reading("drift", 1.0),
                            "volume": reading("volume", 3.0)}, CompositeConfig())
    assert r == pytest.approx(0.4 * 2 + 0.3 * 1 + 0.3 * 3) and not deg and not nr


def test_composite_missing_component_is_degraded_not_zero_silently():
    r, deg, nr = composite({"panic": reading("panic", None, False), "drift": reading("drift", 1.0),
                            "volume": reading("volume", 1.0)}, CompositeConfig())
    assert deg and "panic unavailable" in deg[0] and not nr
    assert r == pytest.approx(0.6)


def test_composite_missing_required_is_not_ready():
    r, deg, nr = composite({"panic": reading("panic", 1.0), "drift": reading("drift", 1.0),
                            "volume": reading("volume", None, False)}, CompositeConfig())
    assert nr and "volume" in nr[0]


def test_composite_nothing_available():
    r, deg, nr = composite({}, CompositeConfig(required_for_ready=[]))
    assert r is None and "no risk component available" in nr


def test_component_reading_rejects_nan():
    with pytest.raises(ValueError):
        ComponentReading(name="panic", available=True, z=float("nan"))


# ---------------------------------------------------------------- cusum
def test_cusum_detects_sustained_shift_and_resets():
    d = CusumDetector(CusumConfig(k=1.0, h=6.0, alarm_hold_minutes=30))
    t = T0
    for _ in range(50):
        r = d.update(t, 0.5)
        t += timedelta(minutes=5)
        assert not r.alarm
    alarms = []
    for _ in range(10):
        r = d.update(t, 3.0)
        alarms.append(r.alarm)
        t += timedelta(minutes=5)
    assert alarms.index(True) == 3  # S: 2,4,6,8>6
    assert r.alarm_active
    later = d.update(t + timedelta(hours=2), 0.0)
    assert not later.alarm_active


def test_cusum_skips_missing():
    d = CusumDetector(CusumConfig())
    d.update(T0, 5.0)
    r = d.update(T0 + timedelta(minutes=5), None)
    assert r.statistic == 4.0 and "not updated" in r.detail
    r = d.update(T0 + timedelta(minutes=10), float("nan"))
    assert r.statistic == 4.0


# ---------------------------------------------------------------- panic
def test_panic_weighted_mean_and_min_count():
    p = PanicAggregator(PanicConfig(window_minutes=60, min_headlines=3))
    p.add(T0, 0.9, 2.0, "a")
    p.add(T0 + timedelta(minutes=1), 0.1, 1.0, "b")
    r = p.compute(T0 + timedelta(minutes=10))
    assert r.value is None and "only 2" in r.reason
    p.add(T0 + timedelta(minutes=2), 0.1, 1.0, "c")
    r = p.compute(T0 + timedelta(minutes=10))
    assert r.value == pytest.approx((1.8 + 0.1 + 0.1) / 4.0)
    assert r.top[0][0] == "a"
    assert p.compute(T0 + timedelta(minutes=70)).value is None  # window moved on


def test_panic_rejects_invalid():
    p = PanicAggregator(PanicConfig())
    assert not p.add(T0, float("nan"), 1.0, "x")
    assert not p.add(T0, 1.5, 1.0, "x")
    assert not p.add(T0, 0.5, 0.0, "x")


# ---------------------------------------------------------------- drift
def unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def test_drift_detects_topic_change():
    cfg = DriftConfig(recent_window_minutes=60, reference_window_days=7, min_recent=3, min_reference=10)
    d = DriftTracker(cfg, dim=3)
    rng = np.random.default_rng(1)
    for i in range(100):
        d.add(T0 + timedelta(minutes=30 * i), unit([1, 0, 0] + rng.normal(0, 0.05, 3)))
    t = T0 + timedelta(minutes=30 * 100)
    for i in range(5):
        d.add(t + timedelta(minutes=5 * i), unit([1, 0, 0] + rng.normal(0, 0.05, 3)))
    same = d.compute(t + timedelta(minutes=25)).value
    t2 = t + timedelta(hours=3)
    for i in range(5):
        d.add(t2 + timedelta(minutes=5 * i), unit([0, 1, 0] + rng.normal(0, 0.05, 3)))
    shifted = d.compute(t2 + timedelta(minutes=25)).value
    assert same < 0.02 and shifted > 0.5


def test_drift_unavailable_when_sparse_and_rejects_bad_vectors():
    d = DriftTracker(DriftConfig(min_recent=5, min_reference=50), dim=3)
    assert not d.add(T0, np.zeros(3))
    assert not d.add(T0, np.array([np.nan, 1, 0]))
    assert not d.add(T0, np.ones(4))
    assert d.rejected == 3
    r = d.compute(T0)
    assert r.value is None and "insufficient" in r.reason


def test_drift_out_of_order_insert_matches_in_order():
    cfg = DriftConfig(min_recent=1, min_reference=1)
    rng = np.random.default_rng(2)
    vecs = [(T0 + timedelta(minutes=10 * i), rng.normal(size=4)) for i in range(60)]
    a, b = DriftTracker(cfg, 4), DriftTracker(cfg, 4)
    for t, v in vecs:
        a.add(t, v)
    shuffled = list(vecs)
    rng.shuffle(shuffled)
    for t, v in shuffled:
        b.add(t, v)
    t = T0 + timedelta(minutes=600)
    assert a.compute(t).value == pytest.approx(b.compute(t).value)
    c = DriftTracker(cfg, 4)
    c.add_many(shuffled[:30])
    c.add_many(shuffled[30:])
    assert c.compute(t).value == pytest.approx(a.compute(t).value)


def test_drift_degenerate_centroid():
    d = DriftTracker(DriftConfig(min_recent=2, min_reference=2), dim=2)
    d.add(T0, np.array([1.0, 0]))
    d.add(T0 + timedelta(minutes=1), np.array([-1.0, 0]))
    d.add(T0 + timedelta(hours=2), np.array([1.0, 0]))
    d.add(T0 + timedelta(hours=2, minutes=1), np.array([0, 1.0]))
    r = d.compute(T0 + timedelta(hours=2, minutes=5))
    assert r.value is None and "degenerate" in r.reason


# ---------------------------------------------------------------- intensity
def intensity_with_history(days=5, per_hour=10, tz="UTC", split=True):
    it = IntensityTracker(IntensityConfig(interval_minutes=60, min_history_days=3, baseline_timezone=tz,
                                          split_weekends=split), NormalizerConfig())
    it.set_coverage_start(T0)
    for h in range(days * 24):
        for k in range(per_hour):
            it.add(T0 + timedelta(hours=h, minutes=k * 60 / per_hour + 1))
    return it


def test_intensity_warmup_then_ready():
    it = intensity_with_history(days=2)
    r = it.compute(T0 + timedelta(days=2))
    assert r.z is None and "warm-up" in r.reason
    it = intensity_with_history(days=5)
    r = it.compute(T0 + timedelta(days=4, hours=12))
    assert r.history_days == 4 and r.z == 0.0 and r.count == 10


def test_intensity_spike_is_high_z():
    it = intensity_with_history(days=5)
    t = T0 + timedelta(days=4, hours=12)
    for k in range(50):
        it.add(t - timedelta(minutes=k))
    r = it.compute(t)
    assert r.count == 60 and r.z > 5


def test_intensity_weekend_split():
    # T0 is Monday; Saturday = day 5 has no prior weekend days -> not ready
    it = intensity_with_history(days=6)
    r = it.compute(T0 + timedelta(days=5, hours=12))
    assert r.z is None and "weekend" in r.reason
    it = intensity_with_history(days=6, split=False)
    assert it.compute(T0 + timedelta(days=5, hours=12)).z is not None


def test_intensity_gap_excludes_baseline_and_current():
    it = intensity_with_history(days=5)
    it.mark_gap(T0 + timedelta(days=3, hours=11), T0 + timedelta(days=3, hours=13))
    r = it.compute(T0 + timedelta(days=4, hours=12))
    assert r.history_days == 3
    it.mark_gap(T0 + timedelta(days=4, hours=11, minutes=30), T0 + timedelta(days=4, hours=11, minutes=40))
    r = it.compute(T0 + timedelta(days=4, hours=12))
    assert r.z is None and "not fully covered" in r.reason


def test_intensity_dst_uses_local_wall_clock():
    it = IntensityTracker(IntensityConfig(baseline_timezone="America/New_York"), NormalizerConfig())
    # US DST starts 2026-03-08. 10:00 New York is 14:00Z on Mar 9 and 15:00Z on Mar 6.
    t = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
    prev = it.same_local_time_days_ago(t, 3)
    assert prev == datetime(2026, 3, 6, 15, 0, tzinfo=UTC)
    it_utc = IntensityTracker(IntensityConfig(), NormalizerConfig())
    assert it_utc.same_local_time_days_ago(t, 3) == datetime(2026, 3, 6, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------- backends
def test_label_resolution_from_config():
    assert resolve_negative_label({0: "positive", 1: "negative", 2: "neutral"}) == 1
    assert resolve_negative_label({"0": "NEG", "1": "POS"}) == 0
    with pytest.raises(RuntimeError):
        resolve_negative_label({0: "LABEL_0", 1: "LABEL_1"})


def test_lexicon_fallback_is_labelled_and_directional():
    s = LexiconSentiment()
    assert s.is_fallback and s.implementation.startswith("fallback-")
    neg, neu, pos = s.negative_probs(["Bank run fears spark panic and selloff", "Committee meets on Tuesday",
                                      "Shares rally to record on strong profits"])
    assert neg > neu > pos
    assert all(0 < p < 1 for p in (neg, neu, pos))


def test_hashing_embedder_deterministic_unit_vectors():
    e = HashingEmbedder(64)
    assert e.is_fallback and e.implementation.startswith("fallback-")
    a = e.embed(["central bank raises rates", "central bank raises rates", ""])
    assert np.allclose(a[0], a[1]) and math.isclose(float(np.linalg.norm(a[0])), 1.0, rel_tol=1e-5)
    assert float(np.linalg.norm(a[2])) == 0.0  # empty text -> zero vector, rejected by drift tracker
