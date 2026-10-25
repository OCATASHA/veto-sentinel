"""Pre-computed evaluation runs of the replayed fixture, for the "same news, two settings" chart.

``vetosentinel evaluate --out reports/<name>`` writes ``<stem>_report.json`` and
``<stem>_scores.json``. Only runs whose recorded fixture SHA-256 equals the replayed file's are
offered, so the chart can only ever show results computed from exactly this data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from vetosentinel.timeutil import parse_iso_utc

REPO = Path(__file__).resolve().parents[3]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _minutes_after(onset: str, minutes: float | None) -> str | None:
    return None if minutes is None else (parse_iso_utc(onset) + timedelta(minutes=minutes)).isoformat()


def _first(events: list[dict[str, Any]], key: str) -> str | None:
    """Earliest time at which any event reached ``key`` (e.g. minutes to HALT evidence)."""
    times = [t for sc in events if (t := _minutes_after(sc["onset"], sc.get(key)))]
    return min(times) if times else None


@lru_cache(maxsize=4)
def comparison(fixture_path: str, roots: tuple[str, ...] = ()) -> dict[str, Any]:
    """Evaluation runs of this fixture: each run's score series and first evidence times."""
    sha = _sha256(fixture_path)
    dirs = [Path(r) for r in roots] or [Path.cwd() / "reports", REPO / "reports"]
    seen: set[Path] = set()
    runs = []
    for root in dirs:
        for rep_path in sorted(root.glob("*/*_report.json")):
            if rep_path.resolve() in seen:
                continue
            seen.add(rep_path.resolve())
            try:
                rep = json.loads(rep_path.read_text(encoding="utf-8"))
                if rep.get("fixture", {}).get("sha256") != sha:
                    continue
                scores = json.loads(rep_path.with_name(rep_path.name.replace("_report.json", "_scores.json"))
                                    .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # stress events only: a data-gap scenario tests the fail-safe, not detection
            events = [sc for sc in rep.get("scenarios") or [] if sc.get("kind") != "data_gap"]
            peaks = [sc["peak_risk_score"] for sc in events if sc.get("peak_risk_score") is not None]
            runs.append({
                "id": rep_path.parent.name, "run_id": rep.get("run_id"),
                "focus": (rep.get("focus") or {}).get("text") or "",
                "fallback": any(v.get("is_fallback") for v in (rep.get("implementations") or {}).values()),
                "events": [sc.get("name") for sc in events],
                "first_caution_evidence": _first(events, "minutes_to_caution_evidence"),
                "first_halt_evidence": _first(events, "minutes_to_halt_evidence"),
                "peak_risk_score": max(peaks) if peaks else None,
                "series": [[r["as_of"], r["risk_score"], r["state"]] for r in scores],
            })
    return {"fixture_sha256": sha, "runs": runs}
