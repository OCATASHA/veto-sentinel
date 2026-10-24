"""Relevance to a chosen focus ("Optimize for ..."): how much each headline counts.

A focus is a few comma-separated phrases ("OPEC, crude oil supply, oil tankers"). Each headline
gets a relevance r in [0, 1], read as the probability that it is about the focus, and enters
every feature with weight

    w = b + (1 - b) * r          b = background_weight (off-focus news still counts a little,
                                 so a market-wide shock is still noticed)

Semantic relevance (MiniLM vectors):
    s = max_k cos(e, q_k)                headline vector vs. each focus phrase vector
    z = (s - m) / sd                     m, sd = median and 1.4826 * MAD of s over recent
                                         headlines (most are off-topic), sd >= min_scale
    r = 1 / (1 + exp(-(z - z_mid) / z_soft))
The calibration (m, sd) is fitted once, on headlines already seen (causal), then frozen, so a
headline's weight never changes after the fact and the baselines stay consistent. Measured on
6,000 live GDELT headlines (2026-10-24): about 2% reach r >= 0.5 for focuses such as
"OPEC, crude oil prices, oil supply, refineries, oil tankers" or "banks, bank run, deposits".

Keyword relevance (FALLBACK, used when only the hashing embedder is available: hashed vectors
collide too often to measure topic similarity): r = the largest fraction of a phrase's words
found in the headline (plural/verb endings tolerated).

How the weights enter the statistics (relevance acts like Poisson thinning: a headline belongs
to the focus with probability ~ w):
    panic   P = sum(a w p) / sum(a w)                         a = source weight
    drift   weighted centroids, unbiased norm ||mu||^2 = (||S||^2 - W2) / (W^2 - W2),
            S = sum(w v), W = sum(w), W2 = sum(w^2)           (= the unweighted formula at w = 1)
    volume  C = sum(c w); the Poisson scale floor 1/sqrt(1 + median C) still applies
Sample-size checks use the effective count n_eff = W^2 / W2. Every baseline is recomputed with
the same weights when the focus changes, so z-scores compare like with like.

Effect on sensitivity: for a weighted mean, a shift d in a subset A of the headlines gives
z = d * sum_A(w) / (sigma * sqrt(sum w^2)). Compared with the whole-market setting (all w = 1),
news about the focus therefore weighs sqrt(n / sum w^2) times as much, and off-focus news
b * sqrt(n / sum w^2) times as much. ``effect`` reports both from recent headlines.
"""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np

from vetosentinel.config import FocusConfig
from vetosentinel.features.backends import EmbeddingBackend

MAX_PHRASES = 12
MAX_PHRASE_CHARS = 80
_SPLIT = re.compile(r"[,;\n]+")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "and", "or", "the", "of", "in", "on", "for", "to", "with", "by", "at", "from",
         "as", "is", "are", "about", "into", "over", "vs"}


def parse_focus(text: str) -> list[str]:
    """Split a focus into distinct phrases (commas, semicolons or new lines)."""
    out: list[str] = []
    for part in _SPLIT.split(text or ""):
        p = " ".join(part.split())[:MAX_PHRASE_CHARS]
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return out[:MAX_PHRASES]


def _words(text: str) -> list[str]:
    return [w for w in _TOKEN.findall(text.lower()) if w not in _STOP]


def _variants(word: str) -> set[str]:
    """The word and its prefixes of 4+ letters, at most 3 letters shorter. Two words match when
    their variants overlap: "banks" ~ "banking" (via "bank"), but "ban" !~ "banks"."""
    return {word} | {word[:k] for k in range(max(4, len(word) - 3), len(word))}


def _keys(text: str) -> set[str]:
    keys: set[str] = set()
    for w in _words(text):
        keys |= _variants(w)
    return keys


def logistic(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


class RelevanceModel:
    """Relevance of headlines to one focus. ``calibrated`` is False until a semantic focus has
    seen ``calibration_min`` headlines; until then the caller treats every headline as relevant."""

    def __init__(self, text: str, cfg: FocusConfig, embedder: EmbeddingBackend):
        self.phrases = parse_focus(text)
        if not self.phrases:
            raise ValueError("the focus needs at least one word or phrase")
        self.text = ", ".join(self.phrases)
        self.cfg = cfg
        self.background = float(cfg.background_weight)
        self.semantic = not embedder.is_fallback
        self.center: float | None = None
        self.scale: float | None = None
        self.calibration_n = 0
        if self.semantic:
            self.implementation = f"semantic-relevance-v1:{embedder.implementation}"
            self.is_fallback = False
            q = np.asarray(embedder.embed(self.phrases), dtype=np.float32)
            norms = np.linalg.norm(q, axis=1, keepdims=True)
            self._q = q / np.where(norms > 0, norms, 1.0)
            self.calibrated = False
        else:
            self.implementation = "fallback-keyword-relevance-v1"
            self.is_fallback = True
            self._kw = [[(w, _variants(w)) for w in _words(p)] for p in self.phrases]
            self._kw = [p for p in self._kw if p]
            if not self._kw:
                raise ValueError("the focus has no searchable words")
            self.calibrated = True

    # ------------------------------------------------------------------ semantic
    def similarity(self, embs: np.ndarray) -> np.ndarray:
        e = np.asarray(embs, dtype=np.float32)
        if e.size == 0:
            return np.zeros(0, dtype=np.float32)
        norms = np.linalg.norm(e, axis=1, keepdims=True)
        e = e / np.where(norms > 0, norms, 1.0)
        return (e @ self._q.T).max(axis=1)

    def calibrate(self, embs: np.ndarray) -> bool:
        """Fit the typical-headline similarity (median, robust sd) on past headlines; freeze it."""
        if not self.semantic or self.calibrated:
            return True
        if len(embs) < self.cfg.calibration_min:
            return False
        s = self.similarity(embs[-self.cfg.calibration_max:]).astype(np.float64)
        m = float(np.median(s))
        sd = float(np.median(np.abs(s - m)) * 1.4826)
        self.center, self.scale = m, max(sd, self.cfg.min_scale)
        self.calibration_n, self.calibrated = int(len(s)), True
        return True

    def z(self, sims: np.ndarray) -> np.ndarray:
        assert self.center is not None and self.scale is not None
        return (np.asarray(sims, dtype=np.float64) - self.center) / self.scale

    # ------------------------------------------------------------------ both
    def keyword_relevance(self, text: str) -> float:
        keys = _keys(text)  # two words match if they share a 4+ letter stem (banks ~ banking)
        best = 0.0
        for phrase in self._kw:
            hit = sum(1 for _, var in phrase if var & keys)
            best = max(best, hit / len(phrase))
        return best

    def relevance(self, texts: list[str], embs: list[np.ndarray | None]) -> np.ndarray:
        """r in [0, 1] per headline (a headline without a vector counts as off-focus)."""
        if not self.semantic:
            return np.asarray([self.keyword_relevance(t) for t in texts], dtype=np.float64)
        r = np.zeros(len(texts), dtype=np.float64)
        idx = [i for i, e in enumerate(embs) if e is not None]
        if idx and self.calibrated:
            sims = self.similarity(np.stack([embs[i] for i in idx]))  # type: ignore[misc]
            r[idx] = logistic((self.z(sims) - self.cfg.z_mid) / self.cfg.z_soft)
        return r

    def weights(self, r: np.ndarray) -> np.ndarray:
        return self.background + (1.0 - self.background) * np.asarray(r, dtype=np.float64)

    def effect(self, r: np.ndarray, counts: np.ndarray | None = None, hours: float = 24.0) -> dict[str, Any]:
        """Share of news about the focus and how much more/less it weighs than whole-market."""
        n = int(len(r))
        if n == 0:
            return {"headlines": 0, "share": None, "per_hour": None, "focus_multiplier": None,
                    "other_multiplier": None, "effective_share": None}
        w = self.weights(r)
        c = np.ones(n) if counts is None else np.asarray(counts, dtype=np.float64)
        mult = math.sqrt(n / float(np.sum(w * w)))
        return {"headlines": n, "share": float(np.mean(r)), "per_hour": float(np.sum(c * r)) / hours,
                "focus_multiplier": mult, "other_multiplier": self.background * mult,
                "effective_share": float(np.sum(r) / np.sum(w))}

    def info(self) -> dict[str, Any]:
        return {"text": self.text, "phrases": self.phrases, "implementation": self.implementation,
                "is_fallback": self.is_fallback, "background_weight": self.background,
                "calibrated": self.calibrated, "calibration": {
                    "headlines": self.calibration_n, "center": self.center, "scale": self.scale,
                    "z_mid": self.cfg.z_mid, "z_soft": self.cfg.z_soft} if self.semantic else None}
