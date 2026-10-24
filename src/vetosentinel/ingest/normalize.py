"""Normalisation of untrusted adapter output into validated ``Headline`` records.

* Canonical URL: lower-case scheme/host, drop ``www.``, fragments, tracking params
  (utm_*, fbclid, gclid, ...), default ports and trailing slashes; sort remaining params.
* Text fingerprint: NFKC, lower-case, strip a trailing " - Source"/" | Source" syndication
  suffix, drop punctuation, collapse whitespace; sha256 -> 16 hex chars.
* headline_id: ``u:<hash(canonical_url)>`` when a URL exists, else ``t:<text fingerprint>``.
* Timestamps: ``published_at`` keeps the provider time (UTC) when parseable and plausible.
  ``event_time`` is what the features window on: published_at, or fetched_at when the
  published time is missing/unparseable/in the future (each case flagged explicitly).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from vetosentinel.models import DataOrigin, Headline, RawItem, TimestampQuality

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "ocid", "cmpid", "mc_cid", "mc_eid", "ref", "rss", "src",
                  "smid", "taid", "mod", "cid", "guccounter", "_ga", "yclid", "igshid"}
_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{2,60}$")
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
         if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, urlencode(sorted(q)), ""))


def clean_headline(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _CTRL.sub(" ", text)
    return _WS.sub(" ", text).strip()


def text_fingerprint(text: str) -> str:
    t = clean_headline(text).lower()
    t = _SUFFIX.sub("", t)
    t = _NONWORD.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


_GDELT_TS = re.compile(r"^(\d{8})T(\d{6})Z$")
_COMPACT_TS = re.compile(r"^\d{14}$")


def parse_timestamp(raw: str | None) -> tuple[datetime | None, TimestampQuality]:
    """Parse a provider timestamp. Returns (utc_datetime_or_None, quality)."""
    if raw is None or not str(raw).strip():
        return None, TimestampQuality.MISSING
    s = str(raw).strip()
    try:
        if m := _GDELT_TS.match(s):
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC), \
                TimestampQuality.PROVIDER
        if _COMPACT_TS.match(s):
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=UTC), TimestampQuality.ASSUMED_UTC
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            dt = parsedate_to_datetime(s)  # RFC 822 (RSS)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC), TimestampQuality.ASSUMED_UTC
        return dt.astimezone(UTC), TimestampQuality.PROVIDER
    except (ValueError, TypeError, OverflowError, IndexError):
        return None, TimestampQuality.UNPARSEABLE


class NormalizationError(ValueError):
    pass


def normalize_item(item: RawItem, *, provider: str, data_origin: DataOrigin, fetched_at: datetime,
                   source_weights: dict[str, float], default_weight: float,
                   future_skew: timedelta = timedelta(minutes=5)) -> Headline:
    if not item.headline or not clean_headline(item.headline):
        raise NormalizationError("empty headline")
    text = clean_headline(item.headline)[:500]
    source = clean_headline(item.source or "") or provider
    curl = canonical_url(item.url)
    fp = text_fingerprint(text)
    published, quality = parse_timestamp(item.published_raw)
    if published is not None and item.published_quality_hint is not None \
            and quality is TimestampQuality.PROVIDER:
        quality = item.published_quality_hint
    event_time = published
    if published is None:
        event_time = fetched_at
    elif published > fetched_at + future_skew:
        quality = TimestampQuality.FUTURE_CLAMPED
        event_time = fetched_at
    weight = source_weights.get(source.lower(), source_weights.get(source, default_weight))
    return Headline(
        headline_id=("u:" + _sha(curl)) if curl else ("t:" + fp),
        headline=text, source=source, url=item.url, canonical_url=curl, text_fingerprint=fp,
        published_at=published, fetched_at=fetched_at, event_time=event_time,
        timestamp_quality=quality, language=(item.language or "und").lower()[:16],
        source_weight=weight, provider=provider, data_origin=data_origin,
        count_weight=float(item.extra.get("count_weight", 1.0)),
    )
