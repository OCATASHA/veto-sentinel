"""Live GDELT adapter built on the 15-minute GKG raw files (replaces the rate-limited DOC API).

Why: the DOC search API answers simple queries but refuses our multi-term finance query with
HTTP 429 ("limit requests to one every 5 seconds") even when called far less often. The raw files
are static downloads announced in ``lastupdate.txt`` every 15 minutes, with no query limits.

* ``fetch(now)`` reads ``lastupdate.txt``, downloads every new 15-minute file since the last one
  it processed (catching up on at most ``max_catchup`` missed files; older misses are recorded as
  data gaps), verifies the newest file against the MD5 GDELT publishes, and returns its
  finance-themed headlines.
* ``backfill(now)`` loads the past ``backfill_days`` from the archive at one file per hour, so the
  monitor knows what "normal" looks like immediately instead of after days of running. Each hourly
  headline carries ``count_weight=4`` (it stands for four 15-minute files) so news-volume
  comparisons stay consistent with live data. Missing archive files become declared gaps.
* GDELT announces files in ``lastupdate.txt`` before they are downloadable; the lag was
  observed at 30-60 minutes. Announced-but-missing files are kept *pending* and retried on every
  poll for up to ``late_grace``; later files are ingested as soon as they exist. Only after the
  grace period is a file treated as skipped (a tiny declared gap). ``data_through`` reports the
  time up to which every file has been ingested (or given up on): live mode assesses the news as
  of that time instead of mistaking not-yet-published news for a quiet hour.
* Only files older than a day are remembered as permanently missing in the cache.
* Parsed files are cached under ``cache_dir`` (a few tens of KB each), so restarts only
  download what is new.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from vetosentinel.ingest.base import USER_AGENT, FetchResult
from vetosentinel.ingest.gkg import BASE, DEFAULT_THEMES, parse_gkg
from vetosentinel.models import DataOrigin, RawItem, TimestampQuality

log = logging.getLogger(__name__)

LASTUPDATE = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
STEP = timedelta(minutes=15)
EPS = timedelta(seconds=1)
MISSING_FINAL_AFTER = timedelta(hours=24)  # younger 404s may still appear, so they are retried
LATE_GRACE = timedelta(hours=3)  # how long an announced file may stay undownloadable before it's skipped
# Once files this much newer have arrived, a still-missing file stops holding back data_through
# (it was probably skipped by GDELT); it is still collected if it turns up within LATE_GRACE.
STOP_WAITING_AFTER = timedelta(minutes=60)


class MissingFile(Exception):
    pass


def _stamp(t: datetime) -> str:
    return t.strftime("%Y%m%d%H%M%S")


class GdeltGkgLiveAdapter:
    data_origin = DataOrigin.LIVE

    def __init__(self, name: str = "gdelt-gkg", themes: tuple[str, ...] = DEFAULT_THEMES,
                 backfill_days: float = 0.0, cache_dir: str | Path = "data/gdelt_cache",
                 timeout: float = 60.0, workers: int = 4, max_catchup: int = 8,
                 client: httpx.Client | None = None):
        self.name = name
        self.themes = tuple(themes)
        self.backfill_days = backfill_days
        self.cache_dir = Path(cache_dir)
        self.workers = workers
        self.max_catchup = max_catchup
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout,
                                             follow_redirects=True)
        self.last_ts: datetime | None = None  # newest file announced and scheduled
        self.pending: set[datetime] = set()  # announced but not downloadable yet (holds back data_through)
        self.late: set[datetime] = set()  # still missing, no longer holding back data_through
        self.newest_ingested: datetime | None = None
        self.data_through: datetime | None = None  # all files up to here are ingested or given up

    # ------------------------------------------------------------------ files
    def latest(self) -> tuple[datetime, str]:
        """Return (timestamp, md5) of the newest GKG file announced by GDELT."""
        r = self.client.get(LASTUPDATE)
        r.raise_for_status()
        for line in r.text.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2].endswith(".gkg.csv.zip"):
                stamp = parts[2].rsplit("/", 1)[-1].split(".", 1)[0]
                return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC), parts[1]
        raise ValueError("lastupdate.txt has no GKG entry")

    def rows(self, ts: datetime, md5: str | None = None) -> list[dict]:
        """Parsed finance rows of one file (cached). Raises MissingFile if GDELT has no such file."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached, missing = self.cache_dir / f"{_stamp(ts)}.jsonl", self.cache_dir / f"{_stamp(ts)}.missing"
        if cached.exists():
            return [json.loads(x) for x in cached.read_text(encoding="utf-8").splitlines() if x]
        if missing.exists():
            raise MissingFile(_stamp(ts))
        r = self.client.get(BASE.format(_stamp(ts)))
        if r.status_code == 404:
            if datetime.now(UTC) - ts > MISSING_FINAL_AFTER:
                missing.touch()
            raise MissingFile(_stamp(ts))
        r.raise_for_status()
        if md5 and hashlib.md5(r.content).hexdigest() != md5:
            raise ValueError(f"checksum mismatch for {_stamp(ts)}")
        rows = parse_gkg(r.content, self.themes)
        tmp = cached.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        os.replace(tmp, cached)
        return rows

    @staticmethod
    def items(rows: list[dict], weight: float) -> list[RawItem]:
        return [RawItem(headline=x["headline"], source=x.get("source"), url=x.get("url"),
                        published_raw=x.get("published_at"), published_quality_hint=TimestampQuality.PROVIDER_SEEN,
                        language=x.get("language"), extra={"count_weight": weight}) for x in rows]

    # ------------------------------------------------------------------ live polling
    def fetch(self, now: datetime) -> FetchResult:
        try:
            newest, md5 = self.latest()
        except (httpx.HTTPError, ValueError) as e:
            return FetchResult(ok=False, error=f"lastupdate.txt: {type(e).__name__}: {e}")
        gaps = []
        if self.last_ts is None or newest > self.last_ts:
            t = newest if self.last_ts is None else self.last_ts + STEP
            new = []
            while t <= newest:
                new.append(t)
                t += STEP
            if len(new) > self.max_catchup:  # long outage: don't try to download hours of files
                gaps.append((new[0] - EPS, new[-self.max_catchup - 1] + EPS))
                new = new[-self.max_catchup:]
            self.pending.update(new)
            self.last_ts = newest
            if self.data_through is None:
                self.data_through = new[0] - STEP
        items: list[RawItem] = []
        errors = []
        for ts in sorted(self.pending | self.late):
            try:
                rows = self.rows(ts, md5 if ts == newest else None)
            except MissingFile:
                if datetime.now(UTC) - ts > LATE_GRACE:
                    self.pending.discard(ts)
                    self.late.discard(ts)
                    gaps.append((ts - EPS, ts + EPS))
                continue
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{_stamp(ts)}: {type(e).__name__}: {e}")
                continue
            items += self.items(rows, 1.0)
            self.pending.discard(ts)
            self.late.discard(ts)
            if self.newest_ingested is None or ts > self.newest_ingested:
                self.newest_ingested = ts
        if self.newest_ingested is not None:
            stale = {ts for ts in self.pending if self.newest_ingested - ts >= STOP_WAITING_AFTER}
            self.pending -= stale
            self.late |= stale
        self.data_through = (min(self.pending) - STEP) if self.pending else self.last_ts
        if errors and not items:
            return FetchResult(ok=False, error="; ".join(errors)[:300])
        return FetchResult(ok=True, items=items, gaps=gaps)

    # ------------------------------------------------------------------ warm-up
    def backfill(self, now: datetime, progress: Callable[[int, int], None] | None = None) -> FetchResult:
        if self.backfill_days <= 0:
            return FetchResult(ok=True)
        try:
            newest, _ = self.latest()
        except (httpx.HTTPError, ValueError) as e:
            return FetchResult(ok=False, error=f"lastupdate.txt: {type(e).__name__}: {e}")
        end = newest.replace(minute=0)
        times = []
        t = end - timedelta(days=self.backfill_days)
        while t <= end:
            times.append(t)
            t += timedelta(hours=1)
        results: dict[datetime, list[dict] | None] = {}
        failed = []

        def one(ts: datetime) -> None:
            results[ts] = None
            for k in range(4):  # the hourly file, else another file from the same hour
                try:
                    results[ts] = self.rows(ts - k * STEP)
                    break
                except MissingFile:
                    continue
                except (httpx.HTTPError, ValueError) as e:
                    failed.append(f"{_stamp(ts - k * STEP)}: {e}")
                    break
            if progress:
                progress(len(results), len(times))

        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(one, times))
        if failed:
            log.warning("%d archive file(s) could not be downloaded, e.g. %s", len(failed), failed[0])
        items, gaps = [], []
        for ts in times:
            rows = results.get(ts)
            if rows is None:
                gaps.append((ts - EPS, ts + EPS))
            else:
                items += self.items(rows, 4.0)  # one hourly file stands for four 15-minute files
        self.last_ts = self.data_through = end
        return FetchResult(ok=True, items=items, coverage_start=times[0], gaps=gaps)
