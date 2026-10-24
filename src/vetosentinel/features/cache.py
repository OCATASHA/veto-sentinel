"""On-disk cache of model outputs (FinBERT negativity, MiniLM embeddings), keyed by headline text.

Scoring tens of thousands of backfilled headlines takes minutes on a CPU; the cache makes every
later start near-instant. Entries are keyed by (implementation id, sha1(text)), so a different
model never reuses another model's scores. SQLite is used because several Veto Sentinel processes
(e.g. live and history) may share the file at once. Fallback heuristics are never cached: they
are cheaper than a lookup.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import numpy as np


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class ModelCache:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.con:
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("CREATE TABLE IF NOT EXISTS sentiment (impl TEXT, key TEXT, p REAL, PRIMARY KEY (impl, key))")
            self.con.execute("CREATE TABLE IF NOT EXISTS embedding (impl TEXT, key TEXT, vec BLOB, PRIMARY KEY (impl, key))")

    def _get(self, table: str, col: str, impl: str, keys: list[str]) -> dict:
        out = {}
        with self.lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                q = f"SELECT key, {col} FROM {table} WHERE impl = ? AND key IN ({','.join('?' * len(chunk))})"
                out.update(self.con.execute(q, [impl, *chunk]).fetchall())
        return out

    def _put(self, table: str, impl: str, rows: list[tuple]) -> None:
        if rows:
            with self.lock, self.con:
                self.con.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)", [(impl, *r) for r in rows])


class CachedSentiment:
    def __init__(self, backend, cache: ModelCache):
        self.backend, self.cache = backend, cache
        self.implementation, self.is_fallback = backend.implementation, backend.is_fallback

    def negative_probs(self, texts: list[str]) -> list[float]:
        keys = [_key(t) for t in texts]
        have = self.cache._get("sentiment", "p", self.implementation, list(set(keys)))
        todo = sorted({k: t for k, t in zip(keys, texts, strict=True) if k not in have}.items())
        if todo:
            probs = self.backend.negative_probs([t for _, t in todo])
            fresh = {k: float(p) for (k, _), p in zip(todo, probs, strict=True)}
            self.cache._put("sentiment", self.implementation, list(fresh.items()))
            have.update(fresh)
        return [have[k] for k in keys]


class CachedEmbedder:
    def __init__(self, backend, cache: ModelCache):
        self.backend, self.cache = backend, cache
        self.implementation, self.is_fallback, self.dim = backend.implementation, backend.is_fallback, backend.dim

    def embed(self, texts: list[str]) -> np.ndarray:
        keys = [_key(t) for t in texts]
        have = {k: np.frombuffer(v, dtype=np.float32)
                for k, v in self.cache._get("embedding", "vec", self.implementation, list(set(keys))).items()}
        todo = sorted({k: t for k, t in zip(keys, texts, strict=True) if k not in have}.items())
        if todo:
            vecs = np.asarray(self.backend.embed([t for _, t in todo]), dtype=np.float32)
            fresh = {k: vecs[i] for i, (k, _) in enumerate(todo)}
            self.cache._put("embedding", self.implementation, [(k, v.tobytes()) for k, v in fresh.items()])
            have.update(fresh)
        if not keys:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([have[k] for k in keys])
