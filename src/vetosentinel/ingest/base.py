"""Provider-independent news adapter interface and shared HTTP plumbing.

Adapters return a ``FetchResult`` and must never raise for provider problems (HTTP errors,
rate limits, malformed payloads, outages); those become ``ok=False`` with an error message.
Exponential backoff after failures and per-adapter minimum poll intervals are handled by
``AdapterRunner``, so a broken provider cannot crash or hot-loop the service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from vetosentinel.models import DataOrigin, RawItem

log = logging.getLogger(__name__)

USER_AGENT = "VetoSentinel/0.1 (research prototype; news risk monitor)"


@dataclass
class FetchResult:
    ok: bool
    items: list[RawItem] = field(default_factory=list)
    error: str = ""
    malformed: int = 0
    rate_limited: bool = False
    retry_after_s: float | None = None
    # Backfills only: the earliest time these items fully cover, and known holes inside it.
    coverage_start: datetime | None = None
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)


class NewsAdapter(Protocol):
    name: str
    data_origin: DataOrigin

    def fetch(self, now: datetime) -> FetchResult: ...


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: float = 15.0, client: httpx.Client | None = None) -> tuple[httpx.Response | None, str]:
    """GET that never raises. Returns (response_or_None, error_message)."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        if client is not None:
            r = client.get(url, params=params, headers=hdrs, timeout=timeout)
        else:
            r = httpx.get(url, params=params, headers=hdrs, timeout=timeout, follow_redirects=True)
        return r, ""
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"


def retry_after_seconds(r: httpx.Response) -> float | None:
    v = r.headers.get("Retry-After")
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


@dataclass
class AdapterHealth:
    name: str
    data_origin: str
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    total_items: int = 0
    total_malformed: int = 0
    next_allowed: datetime | None = None


class AdapterRunner:
    """Wraps an adapter with poll interval, backoff and health tracking."""

    def __init__(self, adapter: NewsAdapter, poll_seconds: float, max_backoff_s: float = 1800.0):
        self.adapter = adapter
        self.poll = timedelta(seconds=poll_seconds)
        self.max_backoff = timedelta(seconds=max_backoff_s)
        self.health = AdapterHealth(adapter.name, str(adapter.data_origin))

    def due(self, now: datetime) -> bool:
        return self.health.next_allowed is None or now >= self.health.next_allowed

    def run(self, now: datetime) -> FetchResult:
        h = self.health
        h.last_attempt = now
        try:
            res = self.adapter.fetch(now)
        except Exception as e:  # noqa: BLE001 - adapter bugs must not kill the service
            log.exception("adapter %s raised", self.adapter.name)
            res = FetchResult(ok=False, error=f"adapter exception {type(e).__name__}: {e}")
        h.total_malformed += res.malformed
        if res.ok:
            h.last_success = now
            h.last_error = ""
            h.consecutive_failures = 0
            h.total_items += len(res.items)
            h.next_allowed = now + self.poll
        else:
            h.consecutive_failures += 1
            h.last_error = res.error
            backoff = min(self.poll * (2 ** min(h.consecutive_failures - 1, 10)), self.max_backoff)
            if res.retry_after_s is not None:
                backoff = max(backoff, timedelta(seconds=res.retry_after_s))
            h.next_allowed = now + backoff
            log.warning("adapter %s failed (%d in a row): %s; next attempt after %s",
                        self.adapter.name, h.consecutive_failures, res.error, backoff)
        return res
