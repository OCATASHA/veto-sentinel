"""Trending words: which words are unusually common in the last hour's headlines (explanation only).

For each term, compare its share of headlines in the recent window (t - 1 h, t] with the reference
window (t - 7 d, t - 1 h], the same windows as topic drift:

    y = (weighted) recent headlines containing the term     n = all recent headlines
    r = the same in the reference window                    m = all reference headlines

If the term were as common as before, then given its k = y + r mentions in both windows, the recent
count would be Binomial(k, n / (n + m)) (the exact conditional test for comparing two Poisson
rates). The one-sided p-value P(X >= y) = I_pi(y, k - y + 1) (regularised incomplete beta) is
reported as a normal z. In words: "if this word were as common as last week, how surprising is
this many mentions in the last hour?" Unlike a log-odds score with a prior, a word never seen in
the week before is judged on its actual mentions, which is what a breaking story looks like.

A term is counted once per headline. Terms are words of 3+ letters minus stop words, plus
adjacent word pairs ("bank run") within punctuation; trailing " - Site name" parts of page titles
are dropped. A word is not listed separately when a listed pair already accounts for most of its
mentions, and overlapping pairs merge ("silicon valley bank"). A term must appear in at least
``min_count`` recent headlines from ``min_sources`` outlets, and reach ``min_z`` (default 4, p of
about 3e-5: hundreds of terms are tested every hour). With a focus ("Optimize for"), headlines
count with the same relevance weights as in the risk score (the test then uses weighted counts as
an approximation). Nothing here feeds the score.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from statistics import NormalDist
from typing import Any

from vetosentinel.ingest.normalize import strip_source_suffix

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&'\u2019]*[A-Za-z0-9]|[A-Za-z]{2,}")
_SEGMENT = re.compile(r"[:;,|()\[\]{}\"!?\u2026\u2013\u2014]|\s-\s|\.\s")
_POSSESSIVE = re.compile(r"['\u2019]s$", re.I)
STOP = frozenset("""
a about above after again against all also am amid among an and any are as at be been before being below
between both but by can could did do does doing down during each few for from further had has have having he
her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once
only or other our ours out over own same she should so some such than that the their them then there these
they this those through to too under until up upon very was we were what when where which while who whom why
will with would you your yours via vs per new news says said say report reports reported update updates live
latest today week weeks year years day days month months time times first last next one two three four five
six seven eight nine ten get gets got make makes made take takes see sees amid back still may might must
shall big top how what why here more most much many very well like just even near set sets look looks
inc ltd plc corp co com www http https amp pdf video watch photos read full story stories breaking exclusive
within key adds added moved analysis final spotlight things thing ways way part plus inside despite ahead around
""".split())
MAX_TOKENS = 40


def terms(text: str) -> tuple[tuple[str, ...], dict[str, str]]:
    """The distinct terms of one headline (lower case, interned: they are kept for every recent
    headline), with the spelling seen for each. Word pairs never span punctuation ("collapse:
    FDIC" is not a phrase); possessives are dropped."""
    out: dict[str, None] = {}
    shown: dict[str, str] = {}
    seen = 0
    for seg in _SEGMENT.split(strip_source_suffix(text)):  # "... - Site Name" is not news
        prev = None
        for w in _WORD.findall(seg):
            seen += 1
            if seen > MAX_TOKENS:
                break
            w = _POSSESSIVE.sub("", w).strip("'’")
            low = w.lower()
            if len(low) < 3 or low in STOP or low.isdigit():
                prev = None
                continue
            out[low] = None
            shown.setdefault(low, w)
            if prev and prev != low:
                out[f"{prev} {low}"] = None
                shown.setdefault(f"{prev} {low}", f"{shown[prev]} {w}")
            prev = low
    return tuple(sys.intern(t) for t in out), shown


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for k in range(1, 400):
        k2 = 2 * k
        aa = k * (b - k) * x / ((qam + k2) * (a + k2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + k) * (qab + k) * x / ((a + k2) * (qap + k2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        step = d * c
        h *= step
        if abs(step - 1.0) < 3e-14:
            break
    return h


def log_binom_tail(y: float, k: float, p: float) -> float:
    """log P(X >= y) for X ~ Binomial(k, p), i.e. log I_p(y, k - y + 1); works for weighted counts."""
    if y <= 0:
        return 0.0
    a, b = y, k - y + 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(p) + b * math.log1p(-p)
    if p < (a + 1.0) / (a + b + 2.0):
        return log_front + math.log(_betacf(a, b, p)) - math.log(a)
    upper = math.exp(log_front) * _betacf(b, a, 1.0 - p) / b
    return math.log(max(1.0 - upper, 1e-300))


def burst_z(y: float, n: float, r: float, m: float) -> float:
    """One-sided z for "a larger share of recent headlines than of reference ones" (0 if not)."""
    k, p = y + r, n / (n + m)
    if y <= k * p:
        return 0.0
    log_p = max(log_binom_tail(y, k, p), -690.0)  # ~1e-300
    return -NormalDist().inv_cdf(math.exp(log_p)) if log_p < -1e-12 else 0.0


def trending(recent: Iterable[tuple[tuple[str, ...], dict[str, str], float, str | None]],
             reference: Iterable[tuple[tuple[str, ...], float]], top: int = 8, min_count: int = 3,
             min_sources: int = 2, min_z: float = 4.0) -> list[dict[str, Any]]:
    """Rank terms that are unusually frequent in ``recent`` vs ``reference``.

    Recent items are (terms, spellings, weight, source) per headline; reference items (terms,
    weight). A term must come from at least ``min_sources`` outlets: one site's own name or
    series title repeating is not news trending.
    Returns at most ``top`` dicts: the term as usually written, z, the number of recent headlines
    containing it, and its weighted share of recent vs reference headlines."""
    yc: defaultdict[str, float] = defaultdict(float)
    ycount: Counter[str] = Counter()
    spell: dict[str, Counter[str]] = {}
    sources: defaultdict[str, set[str]] = defaultdict(set)
    n = 0.0
    for ts, shown, w, src in recent:
        n += w
        for t in ts:
            yc[t] += w
            ycount[t] += 1
            sources[t].add((src or "").lower())
        for low, orig in shown.items():
            spell.setdefault(low, Counter())[orig] += 1
    rc: defaultdict[str, float] = defaultdict(float)
    m = 0.0
    for ts, w in reference:
        m += w
        for t in ts:
            rc[t] += w
    if n <= 0 or m <= 0:
        return []
    scored = sorted(((z, t) for t, y in yc.items() if ycount[t] >= min_count and len(sources[t]) >= min_sources
                     and (z := burst_z(y, n, rc.get(t, 0.0), m)) >= min_z), reverse=True)
    picked: list[dict[str, Any]] = []
    for z, t in scored:
        words = t.split(" ")
        if len(words) == 1:  # mostly said as part of a listed pair: the pair says it better
            if any(t in p["parts"] and p["count"] >= 0.75 * ycount[t] for p in picked):
                continue
        else:
            near = [p for p in picked if len(p["parts"]) >= 2
                    and abs(p["count"] - ycount[t]) <= 0.25 * max(p["count"], ycount[t])]
            grown = next((p for p in near if p["parts"][-1] == words[0]), None)
            if grown is not None:  # "silicon valley" + "valley bank" -> "silicon valley bank"
                grown["parts"].append(words[1])
                continue
            grown = next((p for p in near if p["parts"][0] == words[1]), None)
            if grown is not None:
                grown["parts"].insert(0, words[0])
                continue
            picked = [p for p in picked if not (len(p["parts"]) == 1 and p["parts"][0] in words
                                                and ycount[t] >= 0.75 * p["count"])]
        picked.append({"parts": words, "z": z, "count": ycount[t], "share": yc[t] / n,
                       "usual_share": rc.get(t, 0.0) / m})
        if len(picked) >= top:
            break
    def spelled(term: str) -> str:
        return spell[term].most_common(1)[0][0] if term in spell else term

    out = []
    for p in picked:
        parts = p["parts"]
        if len(parts) == 1:
            word = spelled(parts[0])
        else:  # a phrase is spelled as its own word pairs were ("Silicon Valley Bank", not "... bank")
            word = spelled(f"{parts[0]} {parts[1]}")
            for a, b in zip(parts[1:], parts[2:], strict=False):
                word += " " + spelled(f"{a} {b}").split(" ", 1)[-1]
        out.append({"word": word, "z": round(p["z"], 2), "count": p["count"],
                    "sources": len(sources[" ".join(p["parts"][:2])]), "share": round(p["share"], 4),
                    "usual_share": round(p["usual_share"], 5),
                    "ratio": round(p["share"] / p["usual_share"], 1) if p["usual_share"] > 0 else None})
    return out
