"""Offline fixture/replay adapter (no network, no API keys).

File format: JSON Lines. The FIRST line must be a metadata record::

    {"_meta": {"data_origin": "synthetic_fixture" | "historical_dataset", "title": "...",
               "description": "...", "scenarios": [{"name", "onset", "end", "kind"}], ...}}

Every following line is one headline::

    {"headline": "...", "source": "...", "url": "...", "published_at": "<ISO-8601>", "language": "en"}

During replay, ``fetch(now)`` releases the items whose published time falls in
``(previous now, now]``, simulating arrival. Items without a parseable timestamp cannot be
placed on the replay timeline; they are skipped and counted as malformed (never guessed).
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from vetosentinel.ingest.base import FetchResult
from vetosentinel.ingest.normalize import parse_timestamp
from vetosentinel.models import DataOrigin, RawItem


@dataclass
class FixtureMeta:
    data_origin: DataOrigin
    title: str = ""
    description: str = ""
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_synthetic(self) -> bool:
        return self.data_origin is DataOrigin.SYNTHETIC


class FixtureError(ValueError):
    pass


def load_fixture(path: str | Path) -> tuple[FixtureMeta, list[tuple[datetime, RawItem]], int]:
    """Return (meta, time-sorted items, malformed_count)."""
    p = Path(path)
    items: list[tuple[datetime, RawItem]] = []
    malformed = 0
    meta: FixtureMeta | None = None
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if meta is None:
                if not isinstance(rec, dict) or "_meta" not in rec:
                    raise FixtureError(f"{p}: first record must be a _meta header")
                m = rec["_meta"]
                try:
                    origin = DataOrigin(m.get("data_origin"))
                except ValueError as e:
                    raise FixtureError(f"{p}: _meta.data_origin must be one of "
                                       f"{[o.value for o in DataOrigin]}") from e
                if origin is DataOrigin.LIVE:
                    raise FixtureError(f"{p}: a fixture cannot claim data_origin=live_provider")
                meta = FixtureMeta(origin, m.get("title", ""), m.get("description", ""),
                                   list(m.get("scenarios", [])), m)
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("headline"), str):
                malformed += 1
                continue
            ts, _q = parse_timestamp(rec.get("published_at"))
            if ts is None:
                malformed += 1
                continue
            items.append((ts, RawItem(headline=rec["headline"], source=rec.get("source"),
                                      url=rec.get("url"), published_raw=rec.get("published_at"),
                                      language=rec.get("language"))))
    if meta is None:
        raise FixtureError(f"{p}: empty fixture")
    items.sort(key=lambda x: x[0])
    return meta, items, malformed


def load_markers(path: str | Path) -> list[dict[str, str]]:
    """Event markers for a fixture's timeline, from an optional sidecar ``<stem>.markers.json``:
    ``{"markers": [{"t": ISO, "label": "...", "note": "..."}]}``. Context for people reading the
    dashboard only (e.g. when a bank was closed); the monitor never uses them. Bad entries are
    skipped; a missing or unreadable sidecar means no markers."""
    side = Path(path).with_suffix(".markers.json")
    try:
        raw = json.loads(side.read_text(encoding="utf-8")).get("markers", [])
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict) or not isinstance(m.get("label"), str):
            continue
        ts, _q = parse_timestamp(m.get("t"))
        if ts is None:
            continue
        out.append({"t": ts.isoformat(), "label": m["label"][:40], "note": str(m.get("note", ""))[:300]})
    return sorted(out, key=lambda x: x["t"])


class FixtureAdapter:
    def __init__(self, path: str | Path, name: str = "fixture"):
        self.name = name
        self.path = str(path)
        self.meta, self._items, self.malformed = load_fixture(path)
        self._times = [t for t, _ in self._items]
        self.data_origin = self.meta.data_origin
        self._cursor: datetime | None = None

    @property
    def start(self) -> datetime | None:
        return self._times[0] if self._times else None

    @property
    def end(self) -> datetime | None:
        return self._times[-1] if self._times else None

    def __len__(self) -> int:
        return len(self._items)

    def reset(self) -> None:
        self._cursor = None

    def fetch(self, now: datetime) -> FetchResult:
        lo = 0 if self._cursor is None else bisect.bisect_right(self._times, self._cursor)
        hi = bisect.bisect_right(self._times, now)
        self._cursor = now if self._cursor is None or now > self._cursor else self._cursor
        malformed, self.malformed = self.malformed, 0  # report load-time malformed count once
        return FetchResult(ok=True, items=[it for _, it in self._items[lo:hi]], malformed=malformed)
