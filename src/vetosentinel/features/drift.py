"""Semantic drift D(t): cosine distance between headline-embedding centroids.

    recent window    = (t - recent_window, t]
    reference window = (t - reference_days, t - recent_window]   (excludes the recent window)
    D(t) = 1 - cos(centroid(recent), centroid(reference))

Finite-sample bias correction (``bias_correction=True``, default): for n unit vectors with
sum S, the plain centroid norm ||S/n||^2 = ||mu||^2 + (1 - ||mu||^2)/n is inflated by noise, so
small windows look "far" from everything and a busy window looks artificially close. We
therefore use the unbiased estimate ||mu||^2 ~= (||S||^2 - n) / (n (n - 1)) for each window's
norm (the cross term S_r.S_ref/(n_r n_ref) is already unbiased). This is still the cosine
distance between centroids, but comparable across window sizes. Being an estimate, D may be
slightly negative when the windows agree (noise), and is bounded to [-1, 2]. If a corrected norm is <= 0
(the window is too dispersed to define a direction) drift is unavailable.

Focus weights (relevance.py): with weights w_i the centroids become S = sum(w_i v_i) / W and,
since E||S||^2 = W2 + (W^2 - W2) ||mu||^2 (W = sum w, W2 = sum w^2), the unbiased norm is
(||S||^2 - W2) / (W^2 - W2); the cross term divides by W_rec * W_ref. Minimum counts apply to
the effective count W^2 / W2. With all w = 1 every formula above is unchanged.

Embeddings are L2-normalised on insert; zero, NaN or inf vectors are rejected and counted.
Unavailable when the recent window has < ``min_recent`` or the reference window has
< ``min_reference`` vectors, or a centroid is degenerate (near-zero norm).
Higher D => recent coverage is about different things than the trailing week => more risk.

Implementation: vectors are kept in time order with prefix sums so each window sum is O(d).
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from vetosentinel.config import DriftConfig

_EPS = 1e-6


@dataclass(frozen=True)
class DriftResult:
    value: float | None
    recent_count: int
    reference_count: int
    reason: str


class DriftTracker:
    def __init__(self, cfg: DriftConfig, dim: int):
        self.cfg = cfg
        self.dim = dim
        self._t: list[datetime] = []
        self._vecs: list[np.ndarray] = []
        self._w: list[float] = []  # focus relevance weight per vector (1 without a focus)
        self._hid: list[str | None] = []
        self._prefix = np.zeros((1, dim), dtype=np.float64)  # cumulative sum of w * v
        self._pw = np.zeros(1)  # cumulative sum of w
        self._pw2 = np.zeros(1)  # cumulative sum of w^2
        self._dirty = False
        self.rejected = 0

    def add(self, t: datetime, vec: np.ndarray, rel: float = 1.0, hid: str | None = None) -> bool:
        v = np.asarray(vec, dtype=np.float64).reshape(-1)
        if v.shape[0] != self.dim or not np.all(np.isfinite(v)):
            self.rejected += 1
            return False
        n = float(np.linalg.norm(v))
        if n < _EPS:
            self.rejected += 1
            return False
        v = v / n
        if not self._t or t >= self._t[-1]:
            self._t.append(t)
            self._vecs.append(v)
            self._w.append(rel)
            self._hid.append(hid)
            if not self._dirty:
                self._prefix = np.vstack([self._prefix, self._prefix[-1] + rel * v])
                self._pw = np.append(self._pw, self._pw[-1] + rel)
                self._pw2 = np.append(self._pw2, self._pw2[-1] + rel * rel)
        else:  # out-of-order arrival: insert and rebuild lazily
            i = bisect.bisect_right(self._t, t)
            self._t.insert(i, t)
            self._vecs.insert(i, v)
            self._w.insert(i, rel)
            self._hid.insert(i, hid)
            self._dirty = True
        return True

    def add_many(self, items: Sequence[tuple]) -> None:
        """Add (time, vector) or (time, vector, relevance weight, headline_id) items."""
        items = sorted(items, key=lambda x: x[0])
        if items and self._t and items[0][0] < self._t[-1]:
            self._dirty = True
        start = len(self._vecs)
        for it in items:
            t, v = it[0], it[1]
            rel = float(it[2]) if len(it) > 2 else 1.0
            vv = np.asarray(v, dtype=np.float64).reshape(-1)
            if vv.shape[0] != self.dim or not np.all(np.isfinite(vv)) or np.linalg.norm(vv) < _EPS:
                self.rejected += 1
                continue
            vv = vv / np.linalg.norm(vv)
            i = bisect.bisect_right(self._t, t)
            self._t.insert(i, t)
            self._vecs.insert(i, vv)
            self._w.insert(i, rel)
            self._hid.insert(i, it[3] if len(it) > 3 else None)
        if not self._dirty and len(self._vecs) > start:
            w = np.asarray(self._w[start:])
            new = np.cumsum(np.stack(self._vecs[start:]) * w[:, None], axis=0) + self._prefix[-1]
            self._prefix = np.vstack([self._prefix, new])
            self._pw = np.concatenate([self._pw, self._pw[-1] + np.cumsum(w)])
            self._pw2 = np.concatenate([self._pw2, self._pw2[-1] + np.cumsum(w * w)])

    def reweight(self, rel: Callable[[str], float]) -> None:
        """Replace every vector's relevance weight (when the focus changes)."""
        self._w = [1.0 if h is None else rel(h) for h in self._hid]
        self._dirty = True

    def _rebuild(self) -> None:
        if self._vecs:
            w = np.asarray(self._w)
            self._prefix = np.vstack([np.zeros((1, self.dim)), np.cumsum(np.stack(self._vecs) * w[:, None], axis=0)])
            self._pw = np.concatenate([[0.0], np.cumsum(w)])
            self._pw2 = np.concatenate([[0.0], np.cumsum(w * w)])
        else:
            self._prefix = np.zeros((1, self.dim))
            self._pw, self._pw2 = np.zeros(1), np.zeros(1)
        self._dirty = False

    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.cfg.reference_window_days) - timedelta(hours=2)
        i = bisect.bisect_left(self._t, cutoff)
        if i > 256 or (i and i > len(self._t) // 4):
            del self._t[:i]
            del self._vecs[:i]
            del self._w[:i]
            del self._hid[:i]
            self._dirty = True

    def _window(self, lo: datetime, hi: datetime) -> tuple[np.ndarray, float, float]:
        """(sum of w*v, sum of w, sum of w^2) over (lo, hi]."""
        i = bisect.bisect_right(self._t, lo)
        j = bisect.bisect_right(self._t, hi)
        return self._prefix[j] - self._prefix[i], float(self._pw[j] - self._pw[i]), float(self._pw2[j] - self._pw2[i])

    def compute(self, t: datetime) -> DriftResult:
        if self._dirty:
            self._rebuild()
        c = self.cfg
        split = t - timedelta(minutes=c.recent_window_minutes)
        s_rec, w_rec, w2_rec = self._window(split, t)
        s_ref, w_ref, w2_ref = self._window(t - timedelta(days=c.reference_window_days), split)
        # effective counts W^2 / W2 (the plain counts when every weight is 1)
        n_rec = w_rec * w_rec / w2_rec if w2_rec > 0 else 0.0
        n_ref = w_ref * w_ref / w2_ref if w2_ref > 0 else 0.0
        k_rec, k_ref = int(round(n_rec)), int(round(n_ref))
        if n_rec < c.min_recent or n_ref < c.min_reference:
            return DriftResult(None, k_rec, k_ref,
                               f"insufficient samples: recent {k_rec}/{c.min_recent}, "
                               f"reference {k_ref}/{c.min_reference}")
        cross = float(np.dot(s_rec, s_ref)) / (w_rec * w_ref)
        if c.bias_correction and n_rec > 1 and n_ref > 1:
            na2 = (float(np.dot(s_rec, s_rec)) - w2_rec) / (w_rec * w_rec - w2_rec)
            nb2 = (float(np.dot(s_ref, s_ref)) - w2_ref) / (w_ref * w_ref - w2_ref)
        else:
            na2 = float(np.dot(s_rec, s_rec)) / w_rec**2
            nb2 = float(np.dot(s_ref, s_ref)) / w_ref**2
        if na2 < _EPS or nb2 < _EPS:
            return DriftResult(None, k_rec, k_ref, "degenerate centroid (near-zero norm after bias correction)")
        cos = cross / float(np.sqrt(na2 * nb2))
        if c.bias_correction:
            # An estimate, so it may land slightly outside [-1, 1]; clipping would pile calm values
            # onto D=0 and collapse the robust scale. Keep only a loose sanity bound.
            cos = float(np.clip(cos, -1.0, 2.0))
        else:
            cos = float(np.clip(cos, -1.0, 1.0))
        return DriftResult(1.0 - cos, k_rec, k_ref, "")
