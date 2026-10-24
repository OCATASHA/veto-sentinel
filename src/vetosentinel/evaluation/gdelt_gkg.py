"""Build a HISTORICAL replay fixture from GDELT 2.1 GKG raw files (public, no key).

GKG files are published every 15 minutes at
``http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip`` (from 2015-02 onwards).
Each row is one article; we keep rows whose themes match finance/economy prefixes and take
the headline from ``<PAGE_TITLE>`` in the V2EXTRASXML column (not every row has one).

Caveats recorded in the fixture metadata:
  * timestamps are GDELT capture-batch times (15-minute resolution), not publication times;
  * GDELT coverage, language mix and theme tagging change over time;
  * ``every_n`` > 1 samples one file in N to keep downloads manageable (each file is several MB),
    which scales volume down uniformly - keep it constant within a study;
  * scenario labels are supplied by the user (``--event``); none are invented here.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from vetosentinel.ingest.base import USER_AGENT
from vetosentinel.ingest.gkg import BASE, DEFAULT_THEMES, file_times, parse_gkg

__all__ = ["BASE", "DEFAULT_THEMES", "build", "file_times", "parse_gkg"]

log = logging.getLogger(__name__)

def build(start: datetime, end: datetime, out: str, every_n: int = 4,
          themes: tuple[str, ...] = DEFAULT_THEMES, events: list[dict] | None = None,
          max_per_file: int | None = None, timeout: float = 60.0) -> dict:
    times = file_times(start, end, every_n)
    n_ok = n_fail = n_rows = 0
    gaps: list[dict] = []  # failed files are missing data, not quiet periods
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(out).with_suffix(".partial")
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True) as c, \
            tmp.open("w", encoding="utf-8") as f:
        for t in times:
            url = BASE.format(t.strftime("%Y%m%d%H%M%S"))
            try:
                r = c.get(url)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                rows = parse_gkg(r.content, themes)
                if max_per_file is not None:
                    rows = rows[:max_per_file]  # NB: a cap truncates the volume signal
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                log.warning("GKG %s failed: %s", url, e)
                gaps.append({"start": t.isoformat(), "end": (t + timedelta(minutes=15 * every_n)).isoformat(),
                             "reason": f"{type(e).__name__}: {e}"[:200]})
                continue
            n_ok += 1
            for row in rows:
                f.write(json.dumps(row) + "\n")
            n_rows += len(rows)
            log.info("GKG %s: %d rows", t.isoformat(), len(rows))
    meta = {
        "data_origin": "historical_dataset",
        "title": f"GDELT GKG 2.1 finance-themed page titles {start.date()} to {end.date()}",
        "source": "GDELT Project GKG 2.1 raw files (http://data.gdeltproject.org/gdeltv2/)",
        "caveats": ["timestamps are GDELT 15-minute capture-batch times, not publication times",
                    f"sampled one 15-minute file in every {every_n}",
                    f"theme filter: {list(themes)}", "language not filtered by GDELT; 'und' recorded",
                    "no per-file cap" if max_per_file is None else f"at most {max_per_file} rows kept per file"],
        "files_ok": n_ok, "files_failed": n_fail, "rows": n_rows, "gaps": gaps,
        "scenarios": events or [],
        "built_at": datetime.now(UTC).isoformat(),
    }
    lines = tmp.read_text(encoding="utf-8").splitlines()
    lines.sort(key=lambda ln: json.loads(ln)["published_at"])
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_meta": meta}) + "\n")
        for ln in lines:
            f.write(ln + "\n")
    tmp.unlink()
    return meta
