"""Optional NewsAPI-compatible adapter. Credentials come only from an environment variable.

If the variable is unset the adapter reports ``ok=False`` ("disabled: key not set") and the
runner backs off; nothing else in Veto Sentinel depends on it.
"""

from __future__ import annotations

import os
from datetime import datetime

from vetosentinel.ingest.base import FetchResult, http_get, retry_after_seconds
from vetosentinel.models import DataOrigin, RawItem


class NewsApiAdapter:
    data_origin = DataOrigin.LIVE

    def __init__(self, name: str = "newsapi", endpoint: str = "https://newsapi.org/v2/top-headlines",
                 api_key_env: str = "VETOSENTINEL_NEWSAPI_KEY", params: dict[str, str] | None = None,
                 timeout: float = 15.0):
        self.name = name
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.params = params or {"category": "business", "language": "en", "pageSize": "100"}
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.api_key_env))

    def fetch(self, now: datetime) -> FetchResult:
        key = os.environ.get(self.api_key_env)
        if not key:
            return FetchResult(ok=False, error=f"disabled: environment variable {self.api_key_env} not set")
        r, err = http_get(self.endpoint, params=self.params, headers={"X-Api-Key": key}, timeout=self.timeout)
        if r is None:
            return FetchResult(ok=False, error=err)
        if r.status_code == 429:
            return FetchResult(ok=False, error="HTTP 429 rate limited", rate_limited=True,
                               retry_after_s=retry_after_seconds(r) or 900.0)
        try:
            data = r.json()
        except ValueError:
            return FetchResult(ok=False, error=f"HTTP {r.status_code}: non-JSON body")
        if r.status_code != 200 or data.get("status") != "ok":
            # never echo the key; provider messages do not contain it
            return FetchResult(ok=False, error=f"HTTP {r.status_code}: {data.get('code')}: {data.get('message')}")
        items, bad = [], 0
        for a in data.get("articles") or []:
            if not isinstance(a, dict) or not isinstance(a.get("title"), str) or a["title"] == "[Removed]":
                bad += 1
                continue
            src = a.get("source") or {}
            items.append(RawItem(headline=a["title"], source=src.get("name") if isinstance(src, dict) else None,
                                 url=a.get("url"), published_raw=a.get("publishedAt"), language=None))
        return FetchResult(ok=True, items=items, malformed=bad)
