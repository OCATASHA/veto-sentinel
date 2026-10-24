"""GDELT 2.0 DOC API adapter (public, no key).

Endpoint: https://api.gdeltproject.org/api/v2/doc/doc  (mode=artlist, format=json)

Caveats, handled explicitly:
* ``seendate`` is when GDELT first saw the article, not the publisher's time, so it is
  tagged ``TimestampQuality.PROVIDER_SEEN``.
* GDELT asks clients to keep request rates low (about one request per 5 s); the default poll
  interval is minutes, and 429 / Retry-After is honoured by ``AdapterRunner``.
* Errors are sometimes returned as HTTP 200 with a plain-text body; non-JSON is a failure.
* The DOC API only searches a rolling recent window (about 3 months); it cannot backfill
  older historical events. Use ``vetosentinel fetch-gdelt-gkg`` for historical studies.
"""

from __future__ import annotations

from datetime import datetime

from vetosentinel.ingest.base import FetchResult, http_get, retry_after_seconds
from vetosentinel.models import DataOrigin, RawItem, TimestampQuality

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_QUERY = ('(bank OR banks OR "central bank" OR inflation OR recession OR markets OR bonds '
                 'OR "interest rates" OR liquidity OR default) sourcelang:english')

_LANG = {"english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
         "portuguese": "pt", "chinese": "zh", "japanese": "ja", "russian": "ru", "arabic": "ar"}


class GdeltDocAdapter:
    data_origin = DataOrigin.LIVE

    def __init__(self, name: str = "gdelt", query: str | None = None, max_records: int = 100,
                 timespan: str = "1h", timeout: float = 20.0, endpoint: str = DOC_API):
        self.name = name
        self.query = query or DEFAULT_QUERY
        self.max_records = max_records
        self.timespan = timespan
        self.timeout = timeout
        self.endpoint = endpoint

    def fetch(self, now: datetime) -> FetchResult:
        params = {"query": self.query, "mode": "artlist", "format": "json",
                  "maxrecords": str(self.max_records), "timespan": self.timespan, "sort": "datedesc"}
        r, err = http_get(self.endpoint, params=params, timeout=self.timeout)
        if r is None:
            return FetchResult(ok=False, error=err)
        if r.status_code == 429:
            return FetchResult(ok=False, error="HTTP 429 rate limited", rate_limited=True,
                               retry_after_s=retry_after_seconds(r) or 60.0)
        if r.status_code != 200:
            return FetchResult(ok=False, error=f"HTTP {r.status_code}: {r.text[:200]!r}")
        return parse_doc_response(r.text)


def parse_doc_response(text: str) -> FetchResult:
    import json

    body = text.strip()
    if not body:
        return FetchResult(ok=True, items=[])  # GDELT returns empty body for zero matches
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return FetchResult(ok=False, error=f"non-JSON response: {body[:200]!r}")
    if not isinstance(data, dict):
        return FetchResult(ok=False, error="unexpected JSON shape")
    arts = data.get("articles", [])
    if not isinstance(arts, list):
        return FetchResult(ok=False, error="'articles' is not a list")
    items, bad = [], 0
    for a in arts:
        if not isinstance(a, dict) or not isinstance(a.get("title"), str) or not a["title"].strip():
            bad += 1
            continue
        lang = str(a.get("language", "")).strip().lower()
        items.append(RawItem(headline=a["title"], source=a.get("domain"), url=a.get("url"),
                             published_raw=a.get("seendate"),
                             published_quality_hint=TimestampQuality.PROVIDER_SEEN,
                             language=_LANG.get(lang, lang or None),
                             extra={"sourcecountry": a.get("sourcecountry")}))
    return FetchResult(ok=True, items=items, malformed=bad)
