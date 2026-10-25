"""Trending words (log-odds with an informative Dirichlet prior): explanation only."""

from datetime import UTC, datetime, timedelta

from vetosentinel.config import Settings
from vetosentinel.features.backends import HashingEmbedder, LexiconSentiment
from vetosentinel.features.pipeline import FeaturePipeline
from vetosentinel.features.trending import burst_z, log_binom_tail, terms, trending
from vetosentinel.models import DataOrigin, Headline, TimestampQuality


def test_terms_drop_possessives_site_names_and_cross_punctuation_pairs():
    t, shown = terms("Silicon Valley Bank's collapse: FDIC takes over SVB - Example News Site")
    assert {"silicon valley", "valley bank", "bank collapse", "fdic", "svb"} <= set(t)
    assert "collapse fdic" not in t  # pairs never span punctuation
    assert not {"example", "news", "site"} & set(t)  # trailing " - Site name" is not news
    assert len(t) == len(set(t)) and shown["fdic"] == "FDIC" and shown["silicon valley"] == "Silicon Valley"
    assert terms("The new report says it will be up")[0] == ()  # stop words only


def test_burst_z_is_the_exact_two_rate_test():
    import math
    from statistics import NormalDist

    # binomial tail against a direct sum, including a word never seen before (P = p^k)
    k, p = 30, 0.04
    direct = sum(math.comb(k, j) * p**j * (1 - p) ** (k - j) for j in range(5, k + 1))
    assert abs(math.exp(log_binom_tail(5, k, p)) - direct) < 1e-12
    assert abs(log_binom_tail(6, 6, 0.01) - 6 * math.log(0.01)) < 1e-9
    assert burst_z(10, 100, 1000, 10_000) == 0  # same share as before: nothing to report
    assert burst_z(3, 100, 2000, 10_000) == 0  # rarer than before
    new_word = burst_z(6, 100, 0, 10_000)  # 6 mentions of a word never seen in the week before
    assert abs(new_word + NormalDist().inv_cdf((100 / 10_100) ** 6)) < 1e-6 and new_word > 6
    assert burst_z(20, 100, 100, 10_000) > burst_z(5, 100, 25, 10_000) > 0


def _corpus():
    calm = ["Stocks edge higher as investors weigh earnings", "Oil prices steady ahead of OPEC meeting",
            "Central bank keeps rates on hold", "Retail sales rise in February", "Tech shares lead gains"]
    reference = [(terms(calm[i % 5])[0], 1.0) for i in range(5000)]
    burst = ["Silicon Valley Bank shares plunge", "Depositors pull cash from Silicon Valley Bank",
             "Silicon Valley Bank seeks buyer"]
    recent = [(*terms(burst[i % 3]), 1.0, f"outlet{i}") for i in range(12)]
    recent += [(*terms(calm[i % 5]), 1.0, f"calm{i}") for i in range(80)]
    recent += [(*terms(f"House of Widgets weekly digest {i}"), 1.0, "one-site.example") for i in range(6)]
    return recent, reference


def test_a_burst_story_trends_as_one_phrase_and_one_site_does_not():
    recent, reference = _corpus()
    out = trending(recent, reference)
    words = [w["word"] for w in out]
    assert words[0] == "Silicon Valley Bank"  # pairs merged into the phrase, not three separate words
    assert not {"Silicon", "Valley", "Silicon Valley"} & set(words)
    assert not any("Widgets" in w or "digest" in w for w in words)  # one outlet repeating itself
    top = out[0]
    assert top["count"] == 12 and top["sources"] >= 2 and top["usual_share"] == 0 and top["ratio"] is None
    assert all(w["z"] >= 3 for w in out)


def test_weights_follow_the_focus():
    recent, reference = _corpus()
    damped = [(t, s, 0.05 if "silicon" in t else w, src) for t, s, w, src in recent]  # an off-focus story
    words = [w["word"] for w in trending(damped, reference)]
    assert "Silicon Valley Bank" not in words


def test_pipeline_trending_uses_the_drift_windows():
    s = Settings()
    s.panic.backend, s.drift.backend = "lexicon", "hashing"
    p = FeaturePipeline(s, LexiconSentiment(), HashingEmbedder(64))
    t0 = datetime(2026, 3, 2, tzinfo=UTC)

    def hl(i, text, t, src):
        return Headline(headline_id=f"h{i}", headline=text, source=src, published_at=t, fetched_at=t, event_time=t,
                        text_fingerprint=f"f{i}", provider="test", data_origin=DataOrigin.SYNTHETIC,
                        timestamp_quality=TimestampQuality.PROVIDER)

    items = [hl(i, f"Markets steady as investors weigh earnings report {i % 7}", t0 + timedelta(minutes=10 * i), f"s{i % 9}")
             for i in range(900)]
    now = items[-1].event_time
    items += [hl(1000 + i, "Regional lender halts withdrawals amid bank run fears", now - timedelta(minutes=5 * i), f"o{i}")
              for i in range(6)]
    p.ingest(items)
    words = [w["word"] for w in p.trending_words(now)]
    assert words and any("withdrawals" in w.lower() or "bank run" in w.lower() for w in words)
    assert p.trending_words(now - timedelta(days=3)) == []  # nothing unusual back then
