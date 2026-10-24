"""Reproducible replay evaluation / event-study report.

Runs the full monitor over a fixture (no network, deterministic given fixture + config +
model implementation) and summarises behaviour around the fixture's labelled scenario
windows. Every number in the report is computed from that run; nothing is hand-entered.

Interpretation guard-rails written into every report:
  * the data origin (synthetic vs historical) is stated first;
  * parameters are labelled as unvalidated starting parameters;
  * results on synthetic data demonstrate mechanics only, never real-world performance;
  * for historical studies, parameters must not be tuned on the evaluated event windows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vetosentinel.config import Settings
from vetosentinel.models import DataOrigin
from vetosentinel.service import Backends, ReplayController
from vetosentinel.store import Store
from vetosentinel.timeutil import parse_iso_utc

SEV = {"NORMAL": 0, "CAUTION": 1, "HALT": 2, "FLATTEN": 3}


@dataclass
class Tick:
    t: datetime
    state: str
    r: float | None
    ready: bool
    degraded: bool
    cp_active: bool = False


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _state_minutes(ticks: list[Tick], step_min: float) -> dict[str, float]:
    out = {k: 0.0 for k in SEV}
    for x in ticks:
        out[x.state] += step_min
    return out


def _episodes(ticks: list[Tick]) -> list[tuple[datetime, datetime, str]]:
    """Contiguous runs where state >= CAUTION, with the peak state."""
    eps, start, peak = [], None, "NORMAL"
    for x in ticks:
        if SEV[x.state] >= 1:
            if start is None:
                start, peak = x.t, x.state
            elif SEV[x.state] > SEV[peak]:
                peak = x.state
        elif start is not None:
            eps.append((start, x.t, peak))
            start = None
    if start is not None:
        eps.append((start, ticks[-1].t, peak))
    return eps


def evaluate(settings: Settings, fixture: str, out_dir: str | None = None,
             db_path: str = ":memory:") -> dict[str, Any]:
    backends = Backends(settings)
    store = Store(db_path)
    ctrl = ReplayController(settings, fixture, store, backends)
    ctrl.run_to_end()
    run_id = ctrl.monitor.run_id
    meta = ctrl.adapter.meta
    step_min = settings.replay.tick_minutes
    rows = store.query("""SELECT as_of, state, risk_score, monitor_ready, degraded, cusum_alarm_active
                          FROM risk_scores WHERE run_id = ? ORDER BY as_of""", [run_id])
    ticks = [Tick(parse_iso_utc(r["as_of"]), r["state"], r["risk_score"], r["monitor_ready"], r["degraded"],
                  bool(r["cusum_alarm_active"])) for r in rows]
    sm = settings.state_machine

    # "Evidence" = the score / change-point would escalate on its own, with a ready monitor and
    # complete data. Separates real signal from fail-safe escalations (warm-up, gaps, outages).
    def caution_evidence(x: Tick) -> bool:
        return x.ready and not x.degraded and x.r is not None and (x.r >= sm.caution_enter or x.cp_active)

    def halt_evidence(x: Tick) -> bool:
        return x.ready and not x.degraded and x.r is not None and (
            x.r >= sm.halt_enter or (x.cp_active and x.r >= sm.caution_enter))
    first_ready = next((x.t for x in ticks if x.ready), None)
    scenarios = []
    windows = []
    for sc in meta.scenarios:
        on, end = parse_iso_utc(sc["onset"]), parse_iso_utc(sc["end"])
        windows.append((on, end))
        inside = [x for x in ticks if on <= x.t <= end]
        before = [x for x in ticks if on - timedelta(minutes=step_min) <= x.t < on]
        state_at_onset = before[-1].state if before else None
        first_c = next((x.t for x in inside if SEV[x.state] >= 1), None)
        first_h = next((x.t for x in inside if SEV[x.state] >= 2), None)
        ev_c = next((x.t for x in inside if caution_evidence(x)), None)
        ev_h = next((x.t for x in inside if halt_evidence(x)), None)
        ready_inside = [x for x in inside if x.ready]
        scenarios.append({
            "name": sc.get("name"), "kind": sc.get("kind"), "onset": on.isoformat(), "end": end.isoformat(),
            "label_source": sc.get("label_source", "fixture metadata"),
            "state_just_before_onset": state_at_onset,
            "minutes_to_caution_or_worse": None if first_c is None else (first_c - on).total_seconds() / 60,
            "minutes_to_halt": None if first_h is None else (first_h - on).total_seconds() / 60,
            "minutes_to_caution_evidence": None if ev_c is None else (ev_c - on).total_seconds() / 60,
            "minutes_to_halt_evidence": None if ev_h is None else (ev_h - on).total_seconds() / 60,
            "minutes_not_ready": sum(step_min for x in inside if not x.ready),
            "minutes_degraded": sum(step_min for x in inside if x.ready and x.degraded),
            "peak_risk_score": max((x.r for x in inside if x.r is not None), default=None),
            "state_minutes": _state_minutes(inside, step_min),
            "minutes_normal_while_degraded_or_not_ready": sum(
                step_min for x in inside if x.state == "NORMAL" and (x.degraded or not x.ready)),
            "monitor_ready_fraction": (len(ready_inside) / len(inside)) if inside else None,
        })

    def in_any_window(a: datetime, b: datetime) -> bool:
        return any(a <= we and b >= ws for ws, we in windows)

    post = [x for x in ticks if first_ready is not None and x.t >= first_ready]
    eps = _episodes(post)
    outside = [e for e in eps if not in_any_window(e[0], e[1])]
    outside_ticks = [x for x in post if not in_any_window(x.t, x.t)]
    days = max(1e-9, (post[-1].t - post[0].t).total_seconds() / 86400) if post else 0
    transitions = store.query("SELECT count(*) AS n FROM decisions WHERE run_id = ? AND changed", [run_id])[0]["n"]

    report = {
        "title": "Veto Sentinel replay evaluation",
        "data_origin": str(meta.data_origin),
        "synthetic": meta.is_synthetic,
        "disclaimer": (
            "SYNTHETIC DEMO DATA: these results only demonstrate that the pipeline and state machine "
            "behave as designed on an invented scenario. They are NOT evidence of real-world detection "
            "ability, lead time, or profitability."
            if meta.is_synthetic else
            "HISTORICAL REPLAY: results depend on the dataset's coverage and timestamps. Parameters are "
            "unvalidated starting parameters; they must not be tuned on the event windows reported here."),
        "parameter_status": settings.parameter_status,
        "focus": {"text": settings.focus.text, "background_weight": settings.focus.background_weight}
        if settings.focus.text else None,
        "fixture": {"path": fixture, "sha256": _sha256(fixture), "title": meta.title,
                    "items": len(ctrl.adapter), "declared_gaps": meta.raw.get("gaps", []),
                    "caveats": meta.raw.get("caveats", [])},
        "implementations": ctrl.monitor.pipeline.implementations,
        "model_notes": backends.notes,
        "run_id": run_id,
        "ticks": len(ticks), "tick_minutes": step_min,
        "first_monitor_ready": first_ready.isoformat() if first_ready else None,
        "counters": ctrl.monitor.counters,
        "state_transitions": transitions,
        "transitions_per_day_after_warmup": (transitions / days) if days else None,
        "scenarios": scenarios,
        "outside_labelled_windows": {
            "elevated_episodes": len(outside),
            "episodes": [{"start": a.isoformat(), "end": b.isoformat(), "peak": p,
                          "minutes": (b - a).total_seconds() / 60,
                          # still de-escalating from the fail-safe warm-up HALT when the monitor became ready
                          "warm_up_recovery": a == first_ready} for a, b, p in outside],
            "state_minutes": _state_minutes(outside_ticks, step_min),
            "not_ready_minutes": sum(step_min for x in outside_ticks if not x.ready),
            "degraded_minutes": sum(step_min for x in outside_ticks if x.ready and x.degraded),
        },
        "config": settings.model_dump(mode="json"),
    }
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(fixture).stem
        (out / f"{stem}_report.json").write_text(json.dumps(report, indent=2, default=str))
        (out / f"{stem}_report.md").write_text(to_markdown(report))
        history = store.score_history(run_id, 100_000)
        (out / f"{stem}_scores.json").write_text(json.dumps(history, default=str))
    store.close()
    return report


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def to_markdown(r: dict[str, Any]) -> str:
    L = [f"# {r['title']}", ""]
    banner = "SYNTHETIC DEMO DATA - NOT OBSERVED HISTORY" if r["synthetic"] else f"Data origin: {r['data_origin']}"
    L += [f"> **{banner}**", ">", f"> {r['disclaimer']}", "",
          f"- Parameters: **{r['parameter_status']}**",
          (f"- Optimize for: **{r['focus']['text']}** (other news weight {r['focus']['background_weight']})"
           if r.get("focus") else "- Optimize for: whole market (every headline weighted equally)"),
          f"- Fixture: `{r['fixture']['path']}` (sha256 `{r['fixture']['sha256'][:16]}...`, {r['fixture']['items']} items)",
          *[f"- Fixture caveat: {c}" for c in r["fixture"]["caveats"]],
          *[f"- Declared data gap: {g['start']} -> {g['end']} ({g.get('reason', '')})" for g in r["fixture"]["declared_gaps"]],
          f"- Run: `{r['run_id']}`, {r['ticks']} ticks of {r['tick_minutes']:g} min",
          f"- First tick with monitor ready: {_fmt(r['first_monitor_ready'])}",
          f"- State transitions: {r['state_transitions']} "
          f"({_fmt(r['transitions_per_day_after_warmup'])} per day after warm-up)",
          "", "## Implementations", ""]
    for k, v in r["implementations"].items():
        L.append(f"- {k}: `{v['implementation']}`" + ("  **(FALLBACK HEURISTIC)**" if v["is_fallback"] else ""))
    for n in r["model_notes"]:
        L.append(f"  - note: {n}")
    L += ["", "## Labelled scenario windows", "",
          "Two kinds of latency are reported. *State* latency counts any escalation, including fail-safe ones "
          "(warm-up, data gaps). *Evidence* latency counts only ticks where R(t) or the change-point would "
          "escalate by itself, with a ready monitor and complete data.", "",
          "| scenario | kind | state before onset | min to >=CAUTION state | min to HALT state | "
          "min to CAUTION evidence | min to HALT evidence | peak R | NORMAL / CAUTION / HALT min | "
          "not-ready / degraded min | NORMAL while degraded |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in r["scenarios"]:
        m = s["state_minutes"]
        L.append(f"| {s['name']} | {s['kind']} | {_fmt(s['state_just_before_onset'])} | "
                 f"{_fmt(s['minutes_to_caution_or_worse'])} | {_fmt(s['minutes_to_halt'])} | "
                 f"{_fmt(s['minutes_to_caution_evidence'])} | {_fmt(s['minutes_to_halt_evidence'])} | "
                 f"{_fmt(s['peak_risk_score'])} | {m['NORMAL']:.0f} / {m['CAUTION']:.0f} / {m['HALT']:.0f} | "
                 f"{s['minutes_not_ready']:.0f} / {s['minutes_degraded']:.0f} | "
                 f"{s['minutes_normal_while_degraded_or_not_ready']:.0f} |")
    L.append("")
    for s in r["scenarios"]:
        L.append(f"- `{s['name']}` window {s['onset']} -> {s['end']}; label source: {s['label_source']}")
    o = r["outside_labelled_windows"]
    L += ["", "## Outside labelled windows (after warm-up)", "",
          f"- Elevated episodes (>= CAUTION) not overlapping any labelled window: **{o['elevated_episodes']}** "
          f"(of which {sum(e['warm_up_recovery'] for e in o['episodes'])} is the cooling-off from the warm-up HALT)",
          "- State minutes: " + ", ".join(f"{k} {v:.0f}" for k, v in o["state_minutes"].items()),
          f"- Of which monitor not ready (warm-up, e.g. no weekend baseline yet): {o['not_ready_minutes']:.0f} min; "
          f"degraded data: {o['degraded_minutes']:.0f} min", ""]
    for e in o["episodes"][:30]:
        L.append(f"  - {e['start']} -> {e['end']} peak {e['peak']} ({e['minutes']:.0f} min)"
                 + (" [warm-up recovery]" if e["warm_up_recovery"] else ""))
    L += ["", "Note: 'outside labelled windows' episodes are not automatically false alarms on historical "
              "data - unlabelled real stress exists - and on synthetic data they reflect the generator's noise.", ""]
    return "\n".join(L)


__all__ = ["DataOrigin", "evaluate", "to_markdown"]
