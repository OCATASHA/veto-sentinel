import json
from datetime import UTC, datetime, timedelta

import pytest

from vetosentinel.ingest.base import AdapterRunner, FetchResult
from vetosentinel.ingest.dedup import Deduplicator
from vetosentinel.ingest.fixture import FixtureAdapter, FixtureError
from vetosentinel.ingest.gdelt import parse_doc_response
from vetosentinel.ingest.newsapi import NewsApiAdapter
from vetosentinel.ingest.normalize import (
    NormalizationError,
    canonical_url,
    normalize_item,
    parse_timestamp,
    text_fingerprint,
)
from vetosentinel.ingest.rss import parse_feed
from vetosentinel.models import DataOrigin, RawItem, TimestampQuality

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def norm(**kw):
    item = RawItem(**{"headline": "Fed holds rates steady", "source": "wire", **kw})
    return normalize_item(item, provider="test", data_origin=DataOrigin.SYNTHETIC, fetched_at=NOW,
                          source_weights={"reuters": 1.5}, default_weight=1.0)


def test_canonical_url():
    a = canonical_url("HTTP://www.Example.com/news/story/?utm_source=x&id=7&fbclid=abc#frag")
    b = canonical_url("https://example.com/news/story?id=7")
    assert a == b == "https://example.com/news/story?id=7"
    assert canonical_url("javascript:alert(1)") is None
    assert canonical_url("") is None
    assert canonical_url("https://example.com:8443/a") == "https://example.com:8443/a"


def test_text_fingerprint_handles_syndication_suffix_and_case():
    assert text_fingerprint("Fed Holds Rates Steady - Reuters") == text_fingerprint("fed holds rates steady!")
    assert text_fingerprint("Fed holds rates") != text_fingerprint("Fed cuts rates")


@pytest.mark.parametrize("raw,quality,expected", [
    ("2026-03-10T11:00:00Z", TimestampQuality.PROVIDER, datetime(2026, 3, 10, 11, tzinfo=UTC)),
    ("2026-03-10T07:00:00-04:00", TimestampQuality.PROVIDER, datetime(2026, 3, 10, 11, tzinfo=UTC)),
    ("Tue, 10 Mar 2026 11:00:00 GMT", TimestampQuality.PROVIDER, datetime(2026, 3, 10, 11, tzinfo=UTC)),
    ("20260310T110000Z", TimestampQuality.PROVIDER, datetime(2026, 3, 10, 11, tzinfo=UTC)),
    ("2026-03-10 11:00:00", TimestampQuality.ASSUMED_UTC, datetime(2026, 3, 10, 11, tzinfo=UTC)),
    (None, TimestampQuality.MISSING, None),
    ("yesterday-ish", TimestampQuality.UNPARSEABLE, None),
])
def test_parse_timestamp(raw, quality, expected):
    dt, q = parse_timestamp(raw)
    assert q is quality and dt == expected


def test_normalize_timestamps_and_ids():
    h = norm(published_raw="2026-03-10T11:00:00Z", url="https://www.example.com/a?utm_medium=x")
    assert h.event_time == h.published_at == datetime(2026, 3, 10, 11, tzinfo=UTC)
    assert h.fetched_at == NOW and h.headline_id.startswith("u:")
    h = norm(published_raw=None)
    assert h.published_at is None and h.event_time == NOW and h.timestamp_quality is TimestampQuality.MISSING
    assert h.headline_id.startswith("t:")
    h = norm(published_raw="2026-03-11T12:00:00Z")
    assert h.timestamp_quality is TimestampQuality.FUTURE_CLAMPED and h.event_time == NOW
    assert h.published_at == datetime(2026, 3, 11, 12, tzinfo=UTC)  # original kept for audit
    h = norm(source="Reuters")
    assert h.source_weight == 1.5
    with pytest.raises(NormalizationError):
        norm(headline="   ")


def test_dedup_url_and_text():
    d = Deduplicator(timedelta(days=1))
    a = norm(url="https://example.com/a", published_raw="2026-03-10T10:00:00Z")
    b = norm(url="https://www.example.com/a?utm_source=t", headline="Different text",
             published_raw="2026-03-10T10:05:00Z")
    c = norm(url="https://other.com/b", headline="Fed holds rates steady - Other Wire",
             published_raw="2026-03-10T10:06:00Z")
    e = norm(url="https://other.com/c", headline="Something new", published_raw="2026-03-10T10:07:00Z")
    assert not d.check_and_add(a).duplicate
    assert d.check_and_add(b).reason == "canonical_url"
    r = d.check_and_add(c)
    assert r.duplicate and r.reason == "text_fingerprint" and r.original_id == a.headline_id
    assert not d.check_and_add(e).duplicate


def test_dedup_ttl_expiry():
    d = Deduplicator(timedelta(hours=1))
    assert not d.check_and_add(norm(published_raw="2026-03-10T08:00:00Z")).duplicate
    assert not d.check_and_add(norm(published_raw="2026-03-10T10:00:00Z")).duplicate


def test_gdelt_parsing():
    body = json.dumps({"articles": [
        {"url": "https://ex.com/1", "title": "Banks slide", "seendate": "20260310T101500Z",
         "domain": "ex.com", "language": "English"},
        {"url": "https://ex.com/2", "title": ""},
        "garbage",
    ]})
    r = parse_doc_response(body)
    assert r.ok and len(r.items) == 1 and r.malformed == 2
    assert r.items[0].language == "en"
    assert r.items[0].published_quality_hint is TimestampQuality.PROVIDER_SEEN
    assert parse_doc_response("").ok
    bad = parse_doc_response("Your query was too short or too long.")
    assert not bad.ok and "non-JSON" in bad.error
    h = normalize_item(r.items[0], provider="gdelt", data_origin=DataOrigin.LIVE, fetched_at=NOW,
                       source_weights={}, default_weight=1.0)
    assert h.timestamp_quality is TimestampQuality.PROVIDER_SEEN


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>CB</title>
<item><title>Statement on monetary policy</title><link>https://cb.example/a</link>
<pubDate>Tue, 10 Mar 2026 11:00:00 GMT</pubDate></item>
<item><description>no title</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Press release</title><link href="https://cb.example/b"/><updated>2026-03-10T09:00:00Z</updated></entry>
</feed>"""


def test_rss_and_atom_parsing():
    r = parse_feed(RSS, "Example CB")
    assert r.ok and len(r.items) == 1 and r.malformed == 1 and r.items[0].source == "Example CB"
    a = parse_feed(ATOM, "Example CB")
    assert a.ok and a.items[0].url == "https://cb.example/b"
    assert not parse_feed(b"<html><body>oops</body></html>", "x").ok
    assert not parse_feed(b"not xml at all", "x").ok


def test_rss_rejects_entity_expansion():
    bomb = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""
    assert not parse_feed(bomb, "x").ok


def test_newsapi_without_key_is_disabled(monkeypatch):
    monkeypatch.delenv("VETOSENTINEL_NEWSAPI_KEY", raising=False)
    a = NewsApiAdapter()
    assert not a.configured
    r = a.fetch(NOW)
    assert not r.ok and "not set" in r.error


def test_fixture_adapter(tmp_path):
    p = tmp_path / "f.jsonl"
    lines = [{"_meta": {"data_origin": "synthetic_fixture", "title": "t"}},
             {"headline": "b", "published_at": "2026-03-10T10:05:00Z"},
             {"headline": "a", "published_at": "2026-03-10T10:00:00Z"},
             {"headline": "no time"},
             {"nope": 1}]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    f = FixtureAdapter(p)
    assert f.data_origin is DataOrigin.SYNTHETIC and len(f) == 2
    r1 = f.fetch(datetime(2026, 3, 10, 10, 2, tzinfo=UTC))
    assert [i.headline for i in r1.items] == ["a"] and r1.malformed == 3
    r2 = f.fetch(datetime(2026, 3, 10, 10, 10, tzinfo=UTC))
    assert [i.headline for i in r2.items] == ["b"] and r2.malformed == 0
    assert f.fetch(datetime(2026, 3, 10, 11, tzinfo=UTC)).items == []


def test_fixture_cannot_claim_live(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text(json.dumps({"_meta": {"data_origin": "live_provider"}}) + "\n")
    with pytest.raises(FixtureError):
        FixtureAdapter(p)
    p.write_text(json.dumps({"headline": "x"}) + "\n")
    with pytest.raises(FixtureError):
        FixtureAdapter(p)


class Flaky:
    name = "flaky"
    data_origin = DataOrigin.LIVE

    def __init__(self):
        self.calls = 0

    def fetch(self, now):
        self.calls += 1
        if self.calls <= 3:
            raise RuntimeError("boom")
        return FetchResult(ok=True, items=[])


def test_adapter_runner_backoff_and_recovery():
    r = AdapterRunner(Flaky(), poll_seconds=60)
    t = NOW
    res = r.run(t)
    assert not res.ok and "boom" in res.error and r.health.consecutive_failures == 1
    assert not r.due(t + timedelta(seconds=59)) and r.due(t + timedelta(seconds=60))
    r.run(t + timedelta(seconds=60))
    assert r.health.next_allowed == t + timedelta(seconds=60 + 120)
    r.run(t + timedelta(seconds=180))
    res = r.run(t + timedelta(seconds=500))
    assert res.ok and r.health.consecutive_failures == 0 and r.health.last_success is not None


def test_adapter_runner_honours_retry_after():
    class RL:
        name, data_origin = "rl", DataOrigin.LIVE

        def fetch(self, now):
            return FetchResult(ok=False, error="429", rate_limited=True, retry_after_s=900)

    r = AdapterRunner(RL(), poll_seconds=60)
    r.run(NOW)
    assert r.health.next_allowed == NOW + timedelta(seconds=900)
