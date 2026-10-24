"""GDELT 2.1 GKG raw-file parsing, shared by the live adapter and the historical builder.

GDELT publishes one GKG file every 15 minutes at
``http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip`` and announces the newest one in
``lastupdate.txt``. These are static files, so they are not subject to the DOC API's rate limits.
Each row is one article; we keep rows whose themes match finance/banking prefixes and take the
headline from ``<PAGE_TITLE>`` in the V2EXTRASXML column. Timestamps are 15-minute capture-batch
times, not publication times.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from html import unescape

BASE = "http://data.gdeltproject.org/gdeltv2/{}.gkg.csv.zip"
# Narrow finance/banking themes (theme-name prefixes). Broad families such as EPU_POLICY match
# most political news and are deliberately excluded.
DEFAULT_THEMES = ("ECON_STOCKMARKET", "ECON_WORLDCURRENCIES", "ECON_INFLATION", "ECON_INTEREST_RATE",
                  "ECON_BANKRUPTCY", "ECON_DEBT", "ECON_CENTRALBANK", "EPU_CATS_MONETARY_POLICY",
                  "EPU_POLICY_INTEREST_RATES", "WB_318_FINANCIAL_ARCHITECTURE_AND_BANKING",
                  "WB_332_CAPITAL_MARKETS", "WB_1235_CENTRAL_BANKS", "WB_442_INFLATION")
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def file_times(start: datetime, end: datetime, every_n: int) -> list[datetime]:
    t = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    out, i = [], 0
    while t < end:
        if i % every_n == 0:
            out.append(t)
        t += timedelta(minutes=15)
        i += 1
    return out


def parse_gkg(content: bytes, themes: tuple[str, ...]) -> list[dict]:
    rows = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", errors="replace")
    for rec in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(rec) < 27:
            continue
        names = {t.split(",", 1)[0] for t in (rec[7] + ";" + rec[8]).split(";") if t}
        if not any(n.startswith(themes) for n in names):
            continue
        m = _TITLE.search(rec[26])
        if not m:
            continue
        title = unescape(m.group(1)).strip()
        if len(title) < 15:
            continue
        try:
            ts = datetime.strptime(rec[1], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        rows.append({"headline": title, "source": rec[3] or None, "url": rec[4] or None,
                     "published_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "language": "und"})
    return rows
