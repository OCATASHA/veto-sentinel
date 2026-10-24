#!/usr/bin/env python3
"""Generate the SYNTHETIC demo fixture (deterministic, seeded).

EVERYTHING in the output is invented: entities (Northbridge Regional Bank, Arcadia,
Borduria, ...) are fictional, sources are "synthetic-wire-*", URLs use the reserved
``.invalid`` TLD. The file is labelled ``data_origin: synthetic_fixture`` and MUST NOT be
presented as observed history or used to claim real-world performance.

Timeline (UTC), 12 days starting Monday 2026-02-02:
  * days 0-6     calm baseline (warm-up; the first weekend has no weekend history)
  * 2026-02-09   02:00-05:00  feed outage: no headlines at all (tests "missing news != calm")
  * 2026-02-10   14:00        "policy surprise" scenario: moderate burst, ~2h, partly negative
  * 2026-02-12   13:30        "bank stress" scenario: sharp volume/negativity/topic shift, ~10h,
                              then decays over ~12h
  * ~4% of items are re-published syndicated copies (tests dedup)
  * 3 deliberately malformed records (tests malformed handling)

Usage: python scripts/generate_synthetic_fixture.py [--out fixtures/synthetic_demo.jsonl] [--seed N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
DAYS = 12
OUTAGE = (datetime(2026, 2, 9, 2, 0, tzinfo=UTC), datetime(2026, 2, 9, 5, 0, tzinfo=UTC))
POLICY = datetime(2026, 2, 10, 14, 0, tzinfo=UTC)
STRESS = datetime(2026, 2, 12, 13, 30, tzinfo=UTC)

SOURCES = ["synthetic-wire-a", "synthetic-wire-b", "synthetic-wire-c", "synthetic-wire-d"]
_PREFIX = ["Acme", "Borealis", "Cobalt", "Dunmore", "Eastgate", "Fenwick", "Granite", "Halcyon", "Ironvale",
           "Juniper", "Kestrel", "Lumen", "Marlow", "Northwind", "Orchard", "Pinecrest", "Quarry", "Redfern",
           "Silverline", "Thornbury", "Upland", "Vantage", "Westmere", "Yarrow", "Zenith", "Ashford", "Brightwater",
           "Coldharbour", "Driftwood", "Emberly"]
_SUFFIX = ["Industrial", "Foods", "Logistics", "Energy", "Retail", "Pharma", "Telecom", "Software", "Mining",
           "Airlines", "Motors", "Utilities", "Insurance", "Shipping", "Media", "Chemicals"]
COMPANIES = [f"{a} {b}" for a in _PREFIX for b in _SUFFIX]
COUNTRIES = ["Arcadia", "Borduria", "Calvania", "Dorvia", "Elbonia"]
INDICATORS = ["retail sales", "factory output", "consumer confidence", "services activity",
              "housing starts", "export orders", "wage growth", "business sentiment"]
MONTHS = ["January", "December"]
PRODUCTS = ["a new product line", "a regional expansion", "a cost-efficiency programme", "a software platform",
            "a distribution partnership", "a sustainability plan", "a share buyback"]

CALM_TEMPLATES = [
    "{co} reports quarterly revenue of {amt} million, in line with expectations",
    "{co} shares {move} {small}% in quiet trading",
    "{country} {ind} {dir} {small}% in {month}",
    "{country} central bank keeps policy rate at {rate}%, as expected",
    "{co} announces {prod}",
    "Oil prices {move} to ${oil} as traders weigh supply outlook",
    "Government bond yields {move} to {rate}% ahead of {country} data",
    "{co} names new chief financial officer",
    "Analysts expect {small}% growth for {co} this year",
    "{country} inflation {dir} to {rate}%, matching forecasts",
    "{co} shares rally {small}% after strong profit report",
    "{co} shares dip {small}% after cautious guidance",
    "Investors await {country} central bank minutes",
    "{co} completes {amt} million acquisition of smaller rival",
    "{country} trade surplus {dir} to {amt} million in {month}",
    "{co} to hire {n} staff at new {country} site",
]
CALM_MOVES = ["edge higher", "edge lower", "rise", "slip", "move"]
CALM_DIRS = ["rises", "falls", "holds", "improves", "eases"]

POLICY_TEMPLATES = [
    "Borduria central bank unexpectedly raises rates by {bp} basis points",
    "Bordurian currency jumps {small}% after surprise rate hike",
    "Economists question Borduria's surprise {bp}bp hike",
    "Bordurian bond yields surge to {rate}% on unexpected policy tightening",
    "Borduria rate shock sparks volatility in regional currencies",
    "Traders scramble to reprice Borduria rate path after surprise move",
    "Bordurian stocks slide {small}% as surprise hike raises recession concerns",
    "{co} warns Borduria rate shock will hit {country} demand",
]

STRESS_TEMPLATES = [
    "Northbridge Regional Bank shares plunge {pct}% as deposit outflows accelerate",
    "Depositors withdraw {amt} million from Northbridge amid bank run fears",
    "Regional bank stocks tumble {pct}% on contagion concerns",
    "Arcadia central bank calls emergency meeting on banking stress",
    "Credit markets freeze as investors flee regional lenders",
    "Northbridge seeks {amt} million in emergency funding after failed capital raise",
    "Analysts warn of liquidity crunch across mid-sized lenders",
    "Bond market turmoil deepens as flight to safety pushes yields to {rate}%",
    "Ratings agency downgrades Northbridge, citing deposit flight",
    "Arcadian regulators weigh rescue options for Northbridge",
    "Bank shares sink {pct}% in worst day since crisis",
    "Withdrawals spread to {co} lending arm, sources say",
    "Panic selling hits Arcadian financial stocks",
    "Northbridge trading halted after shares collapse {pct}%",
    "Fears of banking contagion spread to {country}",
    "Interbank lending strains as Northbridge crisis escalates",
    "Investors pull {amt} million from regional banks amid turmoil",
    "{co} shares fall {pct}% on exposure to Northbridge",
]

QUALIFIERS = [", report says", " - analysis", ": what to know", ", sources say", " in early trade",
              " in late trade", ", data show", " (update {k})", ", {n} analysts say", " - live {k}",
              " as markets digest news", ", filings show"]


def rate_per_hour(t: datetime) -> float:
    """Calm baseline intensity with a diurnal pattern; weekends at 50%."""
    h = t.hour + t.minute / 60
    diurnal = 14 + 26 * max(0.0, math.sin(math.pi * (h - 7) / 15)) if 7 <= h <= 22 else 14
    if t.weekday() >= 5:
        diurnal *= 0.5
    return diurnal


def scenario_profile(t: datetime) -> tuple[float, float, str | None]:
    """Return (volume multiplier, share of scenario headlines, scenario name)."""
    d = (t - STRESS).total_seconds() / 3600
    if 0 <= d < 34:
        if d < 3:
            share, mult = 0.25 + 0.55 * d / 3, 1.5 + 2.0 * d / 3
        elif d < 10:
            share, mult = 0.8, 3.5
        else:
            decay = math.exp(-(d - 10) / 5)
            share, mult = 0.8 * decay, 1 + 2.5 * decay
        return mult, share, "bank_stress"
    d = (t - POLICY).total_seconds() / 3600
    if 0 <= d < 3:
        share = 0.6 if d < 1.5 else 0.6 * (3 - d) / 1.5
        return 1.0 + 1.2 * (1 - d / 3), share, "policy_surprise"
    return 1.0, 0.0, None


def fill(tpl: str, rng: random.Random) -> str:
    text = tpl.format(co=rng.choice(COMPANIES), country=rng.choice(COUNTRIES), ind=rng.choice(INDICATORS),
                      dir=rng.choice(CALM_DIRS), month=rng.choice(MONTHS), prod=rng.choice(PRODUCTS),
                      move=rng.choice(CALM_MOVES), pct=rng.randint(6, 60), small=f"{rng.uniform(0.1, 4.9):.1f}",
                      rate=f"{rng.uniform(1.5, 6.5):.2f}", amt=rng.randint(12, 990), oil=f"{rng.uniform(55, 95):.2f}",
                      n=rng.randint(3, 900), bp=rng.choice([100, 125, 150, 175]))
    if rng.random() < 0.6:
        text += rng.choice(QUALIFIERS).format(k=rng.randint(1, 9), n=rng.randint(3, 40))
    return text


def poisson(rng: random.Random, lam: float) -> int:
    l_, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l_:
            return k
        k += 1


def generate(seed: int) -> tuple[dict, list[dict]]:
    rng = random.Random(seed)
    rows: list[dict] = []
    step = timedelta(minutes=5)
    t = START
    end = START + timedelta(days=DAYS)
    counter = 0
    while t < end:
        if OUTAGE[0] <= t < OUTAGE[1]:
            t += step
            continue
        mult, share, scen = scenario_profile(t)
        n = poisson(rng, rate_per_hour(t) * mult * step.total_seconds() / 3600)
        for _ in range(n):
            counter += 1
            ts = t + timedelta(seconds=rng.randrange(0, int(step.total_seconds())))
            if scen and rng.random() < share:
                tpl = rng.choice(STRESS_TEMPLATES if scen == "bank_stress" else POLICY_TEMPLATES)
            else:
                tpl = rng.choice(CALM_TEMPLATES)
            text = fill(tpl, rng)
            src = rng.choice(SOURCES)
            slug = hashlib.sha1(f"{counter}{text}".encode()).hexdigest()[:10]
            rows.append({"headline": text, "source": src, "url": f"https://{src}.invalid/story/{slug}",
                         "published_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "language": "en"})
            if rng.random() < 0.04:  # syndicated copy: same text, other outlet, suffix
                other = rng.choice([s for s in SOURCES if s != src])
                rows.append({"headline": f"{text} - {other.replace('-', ' ').title()}", "source": other,
                             "url": f"https://{other}.invalid/syndicated/{slug}",
                             "published_at": (ts + timedelta(minutes=rng.randrange(1, 20))).strftime(
                                 "%Y-%m-%dT%H:%M:%SZ"), "language": "en"})
        t += step
    rows.sort(key=lambda r: r["published_at"])
    # three deliberately malformed records
    rows.insert(100, {"headline": "Record with no timestamp (deliberately malformed)", "source": "synthetic-wire-a"})
    rows.insert(200, {"source": "synthetic-wire-b", "published_at": "2026-02-02T10:00:00Z"})
    rows.insert(300, {"headline": "Record with garbage timestamp", "published_at": "sometime soon"})

    meta = {
        "data_origin": "synthetic_fixture",
        "title": "Veto Sentinel synthetic demo fixture",
        "warning": "SYNTHETIC DEMO DATA. All entities, headlines, sources and URLs are invented. "
                   "Not observed history; results on this file say nothing about real-world performance.",
        "generator": "scripts/generate_synthetic_fixture.py",
        "seed": seed,
        "start": START.isoformat(),
        "end": end.isoformat(),
        "scenarios": [
            {"name": "feed_outage", "kind": "data_gap", "onset": OUTAGE[0].isoformat(),
             "end": OUTAGE[1].isoformat(), "note": "no headlines; monitor must not treat as calm"},
            {"name": "policy_surprise", "kind": "stress", "onset": POLICY.isoformat(),
             "end": (POLICY + timedelta(hours=3)).isoformat()},
            {"name": "bank_stress", "kind": "stress", "onset": STRESS.isoformat(),
             "end": (STRESS + timedelta(hours=34)).isoformat()},
        ],
        "malformed_records": 3,
    }
    return meta, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fixtures/synthetic_demo.jsonl")
    ap.add_argument("--seed", type=int, default=20260202)
    a = ap.parse_args()
    meta, rows = generate(a.seed)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"_meta": meta}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} records (synthetic) to {out}")


if __name__ == "__main__":
    main()
