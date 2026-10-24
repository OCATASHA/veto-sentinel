"""RSS 2.0 / Atom adapter, e.g. for central-bank press-release feeds.

XML is parsed with ``defusedxml`` (untrusted input). Example feeds (availability and terms
are the publishers'; verify before relying on them):
  * Federal Reserve:  https://www.federalreserve.gov/feeds/press_all.xml
  * ECB:              https://www.ecb.europa.eu/rss/press.html
  * Bank of England:  https://www.bankofengland.co.uk/rss/news
"""

from __future__ import annotations

from datetime import datetime
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as DET

from vetosentinel.ingest.base import FetchResult, http_get, retry_after_seconds
from vetosentinel.models import DataOrigin, RawItem

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def _text(el: Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


class RssAdapter:
    data_origin = DataOrigin.LIVE

    def __init__(self, name: str, url: str, source: str | None = None, timeout: float = 15.0):
        self.name = name
        self.url = url
        self.source = source or name
        self.timeout = timeout

    def fetch(self, now: datetime) -> FetchResult:
        r, err = http_get(self.url, timeout=self.timeout)
        if r is None:
            return FetchResult(ok=False, error=err)
        if r.status_code == 429:
            return FetchResult(ok=False, error="HTTP 429 rate limited", rate_limited=True,
                               retry_after_s=retry_after_seconds(r) or 300.0)
        if r.status_code != 200:
            return FetchResult(ok=False, error=f"HTTP {r.status_code}")
        return parse_feed(r.content, self.source)


def parse_feed(content: bytes, source: str) -> FetchResult:
    try:
        root = DET.fromstring(content)
    except Exception as e:  # noqa: BLE001 - ParseError or defusedxml security exceptions
        return FetchResult(ok=False, error=f"XML parse failure: {type(e).__name__}: {e}")
    items: list[RawItem] = []
    bad = 0
    rss_items = root.findall("./channel/item") or root.findall(".//item")
    for it in rss_items:
        title = _text(it.find("title"))
        if not title:
            bad += 1
            continue
        link = _text(it.find("link")) or _text(it.find("guid"))
        pub = _text(it.find("pubDate")) or _text(it.find(f"{_DC}date"))
        items.append(RawItem(headline=title, source=source, url=link, published_raw=pub, language="en"))
    for e in root.findall(f".//{_ATOM}entry"):
        title = _text(e.find(f"{_ATOM}title"))
        if not title:
            bad += 1
            continue
        link_el = e.find(f"{_ATOM}link")
        link = link_el.get("href") if link_el is not None else None
        pub = _text(e.find(f"{_ATOM}published")) or _text(e.find(f"{_ATOM}updated"))
        items.append(RawItem(headline=title, source=source, url=link, published_raw=pub, language="en"))
    if not rss_items and not root.findall(f".//{_ATOM}entry") and root.tag not in ("rss", f"{_ATOM}feed"):
        return FetchResult(ok=False, error=f"not an RSS/Atom document (root <{root.tag}>)")
    return FetchResult(ok=True, items=items, malformed=bad)
