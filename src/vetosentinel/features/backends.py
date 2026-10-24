"""Model backends for sentiment (panic) and embeddings (drift).

Real backends (FinBERT, MiniLM) are used when the optional ``ml`` extras and model weights
are available. Otherwise Veto Sentinel falls back to small deterministic heuristics that are
ALWAYS labelled ``is_fallback=True`` with an implementation id starting ``fallback-``. These
labels propagate to the dashboard, the bus messages, and stored feature rows so a fallback
score can never be mistaken for model output.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class SentimentBackend(Protocol):
    implementation: str
    is_fallback: bool

    def negative_probs(self, texts: list[str]) -> list[float]: ...


class EmbeddingBackend(Protocol):
    implementation: str
    is_fallback: bool
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


# --------------------------------------------------------------------------- FinBERT
def resolve_negative_label(id2label: dict) -> int:
    """Find the index of the negative class from the model config, never by assumption."""
    matches = [int(i) for i, lab in id2label.items() if str(lab).strip().lower() in ("negative", "neg")]
    if len(matches) != 1:
        raise RuntimeError(f"cannot uniquely identify a negative label in id2label={id2label!r}")
    return matches[0]


class FinbertSentiment:
    is_fallback = False

    def __init__(self, model_name: str = "ProsusAI/finbert", batch_size: int = 32):
        import torch  # noqa: F401  (optional dependency)
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.model.eval()
        self.neg_idx = resolve_negative_label(self.model.config.id2label)
        self.batch_size = batch_size
        self.implementation = f"finbert:{model_name}"
        log.info("FinBERT loaded (%s); negative label index=%d from id2label=%s",
                 model_name, self.neg_idx, self.model.config.id2label)

    def negative_probs(self, texts: list[str]) -> list[float]:
        out: list[float] = []
        torch = self._torch
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            enc = self.tokenizer(batch, padding=True, truncation=True, max_length=96, return_tensors="pt")
            with torch.no_grad():
                logits = self.model(**enc).logits
            probs = torch.softmax(logits, dim=-1)[:, self.neg_idx].tolist()
            out.extend(float(p) for p in probs)
        return out


# --------------------------------------------------------------------------- lexicon fallback
_NEG_TERMS = {
    "crash", "crashes", "plunge", "plunges", "plunged", "tumble", "tumbles", "slump", "slumps",
    "collapse", "collapses", "collapsed", "crisis", "panic", "default", "defaults", "bankrupt",
    "bankruptcy", "insolvency", "insolvent", "contagion", "fear", "fears", "turmoil", "selloff",
    "sell-off", "losses", "loss", "downgrade", "downgrades", "downgraded", "warns", "warning",
    "recession", "bailout", "rescue", "emergency", "sanctions", "war", "invasion", "attack",
    "layoffs", "fraud", "probe", "withdrawals", "outflows", "writedown", "write-down", "impairment",
    "volatility", "slide", "slides", "sinks", "plummet", "plummets", "worst", "shock", "uncertainty",
    "concern", "concerns", "stress", "distress", "failure", "fails", "failed", "freeze", "frozen",
    "halted", "suspended", "seized", "unrest", "escalation", "escalates", "strain", "strains",
    "selling", "scramble", "scrambles", "jitters", "rout", "spiral", "contraction", "deficit",
}
_NEG_PHRASES = ("bank run", "deposit flight", "liquidity crunch", "credit crunch", "margin call",
                "fire sale", "risk-off", "flight to safety", "emergency meeting", "emergency funding")
_POS_TERMS = {
    "gain", "gains", "rise", "rises", "rally", "rallies", "surge", "surges", "record", "beat", "beats",
    "growth", "upgrade", "upgrades", "upgraded", "strong", "robust", "recover", "recovers", "recovery",
    "rebound", "rebounds", "optimism", "profit", "profits", "expands", "boost", "boosts", "eases",
    "stable", "steady", "calm", "approves", "approved", "improves", "improved", "resilient",
}
_TOKEN = re.compile(r"[a-z][a-z\-']*")


class LexiconSentiment:
    """FALLBACK HEURISTIC. Small hand-written finance term list; not FinBERT, not validated."""

    implementation = "fallback-lexicon-v1"
    is_fallback = True

    def negative_probs(self, texts: list[str]) -> list[float]:
        return [self._one(t) for t in texts]

    @staticmethod
    def _one(text: str) -> float:
        low = text.lower()
        toks = _TOKEN.findall(low)
        neg = sum(1 for t in toks if t in _NEG_TERMS) + sum(1 for p in _NEG_PHRASES if p in low)
        pos = sum(1 for t in toks if t in _POS_TERMS)
        x = 1.2 * (neg - pos) - 1.5
        return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------- MiniLM
class MiniLMEmbedder:
    is_fallback = False

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        # Newer sentence-transformers renamed this method; either may return None for odd models.
        get_dim = getattr(self.model, "get_embedding_dimension", None) or getattr(self.model, "get_sentence_embedding_dimension")  # noqa: B009 (old name, older versions)
        dim = get_dim()
        if dim is None:
            raise RuntimeError(f"{model_name} does not report an embedding size")
        self.dim = int(dim)
        self.implementation = f"sentence-transformers:{model_name}"

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.asarray(self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False),
                          dtype=np.float32)


# --------------------------------------------------------------------------- hashing fallback
class HashingEmbedder:
    """FALLBACK HEURISTIC. Signed feature hashing of word unigrams+bigrams; lexical, not semantic."""

    is_fallback = True

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.implementation = f"fallback-hashing-v1-d{dim}"

    def _vec(self, text: str) -> np.ndarray:
        toks = _TOKEN.findall(text.lower())
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        v = np.zeros(self.dim, dtype=np.float32)
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            v[idx] += sign
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])


# --------------------------------------------------------------------------- selection
def load_sentiment(backend: str, model_name: str, batch_size: int) -> tuple[SentimentBackend, str]:
    """Return (backend, note). ``note`` explains any fallback."""
    if backend == "lexicon":
        return LexiconSentiment(), "lexicon fallback selected by configuration"
    try:
        return FinbertSentiment(model_name, batch_size), "FinBERT loaded"
    except Exception as e:  # noqa: BLE001 - any failure -> labelled fallback (or error if forced)
        if backend == "finbert":
            raise
        log.warning("FinBERT unavailable (%s: %s); using LABELLED FALLBACK lexicon heuristic",
                    type(e).__name__, e)
        return LexiconSentiment(), f"FinBERT unavailable ({type(e).__name__}: {e}); fallback heuristic in use"


def load_embedder(backend: str, model_name: str, hashing_dim: int) -> tuple[EmbeddingBackend, str]:
    if backend == "hashing":
        return HashingEmbedder(hashing_dim), "hashing fallback selected by configuration"
    try:
        return MiniLMEmbedder(model_name), "MiniLM loaded"
    except Exception as e:  # noqa: BLE001
        if backend == "minilm":
            raise
        log.warning("MiniLM unavailable (%s: %s); using LABELLED FALLBACK hashing embedder",
                    type(e).__name__, e)
        return HashingEmbedder(hashing_dim), f"MiniLM unavailable ({type(e).__name__}: {e}); fallback heuristic in use"
