"""DuckDB persistence for headlines, features, scores, decisions, control events and runs.

All timestamps are stored as naive TIMESTAMP values in UTC (DuckDB TIMESTAMPTZ round-trips
need extra Python dependencies). A single connection is shared behind a lock, so the
analytics loop and the dashboard can live in one process.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from vetosentinel.models import ComponentReading, DecisionRecord, Headline, RiskSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, mode VARCHAR, started_at TIMESTAMP, ended_at TIMESTAMP,
    data_origin VARCHAR, fixture_path VARCHAR, fixture_meta VARCHAR, config VARCHAR,
    implementations VARCHAR, parameter_status VARCHAR);
CREATE TABLE IF NOT EXISTS headlines (
    run_id VARCHAR, headline_id VARCHAR, headline VARCHAR, source VARCHAR, url VARCHAR,
    canonical_url VARCHAR, text_fingerprint VARCHAR, published_at TIMESTAMP, fetched_at TIMESTAMP,
    event_time TIMESTAMP, timestamp_quality VARCHAR, language VARCHAR, source_weight DOUBLE,
    provider VARCHAR, data_origin VARCHAR, p_negative DOUBLE, sentiment_impl VARCHAR, embed_impl VARCHAR);
CREATE TABLE IF NOT EXISTS duplicates (
    run_id VARCHAR, headline_id VARCHAR, original_id VARCHAR, reason VARCHAR, provider VARCHAR,
    headline VARCHAR, event_time TIMESTAMP);
CREATE TABLE IF NOT EXISTS features (
    run_id VARCHAR, as_of TIMESTAMP, component VARCHAR, available BOOLEAN, raw DOUBLE, z DOUBLE,
    z_unclipped DOUBLE, sample_count INTEGER, baseline_count INTEGER, baseline_center DOUBLE,
    baseline_scale DOUBLE, baseline_ready BOOLEAN, reason VARCHAR, implementation VARCHAR,
    is_fallback BOOLEAN);
CREATE TABLE IF NOT EXISTS risk_scores (
    run_id VARCHAR, as_of TIMESTAMP, risk_score DOUBLE, state VARCHAR, monitor_ready BOOLEAN,
    degraded BOOLEAN, degraded_reasons VARCHAR, not_ready_reasons VARCHAR, panic_z DOUBLE,
    drift_z DOUBLE, volume_z DOUBLE, cusum_stat DOUBLE, cusum_alarm_active BOOLEAN,
    top_headline_ids VARCHAR);
CREATE TABLE IF NOT EXISTS decisions (
    run_id VARCHAR, as_of TIMESTAMP, wall_ts TIMESTAMP, prev_state VARCHAR, new_state VARCHAR,
    changed BOOLEAN, rule VARCHAR, reason VARCHAR, risk_score DOUBLE, components VARCHAR,
    changepoint VARCHAR, headline_ids VARCHAR, health VARCHAR);
CREATE TABLE IF NOT EXISTS control_events (
    run_id VARCHAR, ts TIMESTAMP, kind VARCHAR, operator VARCHAR, detail VARCHAR);
CREATE TABLE IF NOT EXISTS ingest_log (
    run_id VARCHAR, ts TIMESTAMP, adapter VARCHAR, ok BOOLEAN, items INTEGER, new_items INTEGER,
    duplicates INTEGER, malformed INTEGER, error VARCHAR);
"""


def _ts(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None)


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.con = duckdb.connect(self.path)
        self.lock = threading.RLock()
        with self.lock:
            self.con.execute(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.con.close()

    # ------------------------------------------------------------------ writes
    def _insert_many(self, table: str, rows: list[tuple]) -> None:
        if not rows:
            return
        width = len(rows[0])
        ph = "(" + ",".join(["?"] * width) + ")"
        with self.lock:
            for i in range(0, len(rows), 400):
                chunk = rows[i:i + 400]
                sql = f"INSERT INTO {table} VALUES " + ",".join([ph] * len(chunk))
                self.con.execute(sql, [v for r in chunk for v in r])

    def start_run(self, run_id: str, mode: str, data_origin: str, config: dict, implementations: dict,
                  parameter_status: str, fixture_path: str | None = None,
                  fixture_meta: dict | None = None) -> None:
        self._insert_many("runs", [(run_id, mode, _ts(datetime.now(UTC)), None, data_origin, fixture_path,
                                    json.dumps(fixture_meta or {}, default=str), json.dumps(config, default=str),
                                    json.dumps(implementations), parameter_status)])

    def end_run(self, run_id: str) -> None:
        with self.lock:
            self.con.execute("UPDATE runs SET ended_at = ? WHERE run_id = ?", [_ts(datetime.now(UTC)), run_id])

    def add_headlines(self, run_id: str, rows: list[tuple[Headline, float | None, str, str]]) -> None:
        self._insert_many("headlines", [
            (run_id, h.headline_id, h.headline, h.source, h.url, h.canonical_url, h.text_fingerprint,
             _ts(h.published_at), _ts(h.fetched_at), _ts(h.event_time), str(h.timestamp_quality), h.language,
             h.source_weight, h.provider, str(h.data_origin), p, si, ei) for h, p, si, ei in rows])

    def add_duplicates(self, run_id: str, rows: list[tuple[Headline, str, str]]) -> None:
        self._insert_many("duplicates", [(run_id, h.headline_id, orig, reason, h.provider, h.headline,
                                          _ts(h.event_time)) for h, orig, reason in rows])

    def add_tick(self, run_id: str, snap: RiskSnapshot, decision: DecisionRecord,
                 cusum_stat: float | None, cusum_active: bool) -> None:
        feats = []
        for c in snap.components.values():
            feats.append(_feature_row(run_id, snap.as_of, c))
        self._insert_many("features", feats)
        z = {k: (v.z if v.available else None) for k, v in snap.components.items()}
        self._insert_many("risk_scores", [(
            run_id, _ts(snap.as_of), snap.risk_score, str(decision.new_state), snap.monitor_ready,
            snap.degraded, json.dumps(snap.degraded_reasons), json.dumps(snap.not_ready_reasons),
            z.get("panic"), z.get("drift"), z.get("volume"), cusum_stat, cusum_active,
            json.dumps(snap.top_headline_ids))])
        self.add_decision(run_id, decision)

    def add_decision(self, run_id: str, d: DecisionRecord) -> None:
        self._insert_many("decisions", [(
            run_id, _ts(d.as_of), _ts(d.wall_ts), str(d.prev_state), str(d.new_state), d.changed, d.rule,
            d.reason, d.risk_score, json.dumps(d.components),
            d.changepoint.model_dump_json() if d.changepoint else None, json.dumps(d.headline_ids),
            json.dumps(d.health, default=str))])

    def add_control_event(self, run_id: str, ts: datetime, kind: str, operator: str, detail: dict) -> None:
        self._insert_many("control_events", [(run_id, _ts(ts), kind, operator, json.dumps(detail, default=str))])

    def add_ingest_log(self, run_id: str, ts: datetime, adapter: str, ok: bool, items: int, new: int,
                       dups: int, malformed: int, error: str) -> None:
        self._insert_many("ingest_log", [(run_id, _ts(ts), adapter, ok, items, new, dups, malformed, error)])

    # ------------------------------------------------------------------ reads
    def query(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        with self.lock:
            cur = self.con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        out = []
        for r in rows:
            rec = {}
            for k, v in zip(cols, r, strict=True):
                rec[k] = v.replace(tzinfo=UTC).isoformat() if isinstance(v, datetime) else v
            out.append(rec)
        return out

    _HISTORY_COLS = ("as_of, risk_score, state, monitor_ready, degraded, panic_z, drift_z, volume_z, "
                     "cusum_stat, cusum_alarm_active")

    def score_history(self, run_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        return self.query(f"""SELECT {self._HISTORY_COLS} FROM risk_scores WHERE run_id = ?
                              ORDER BY as_of DESC LIMIT ?""", [run_id, limit])[::-1]

    def score_history_since(self, run_id: str, since: datetime, limit: int = 5000) -> list[dict[str, Any]]:
        """Rows strictly after ``since`` (oldest first): lets a dashboard poll incrementally."""
        return self.query(f"""SELECT {self._HISTORY_COLS} FROM risk_scores WHERE run_id = ? AND as_of > ?
                              ORDER BY as_of LIMIT ?""", [run_id, _ts(since), limit])

    def transitions(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query("""SELECT as_of, wall_ts, prev_state, new_state, rule, reason, risk_score,
                             components, headline_ids FROM decisions WHERE run_id = ? AND changed
                             ORDER BY as_of DESC LIMIT ?""", [run_id, limit])

    def moment(self, run_id: str, t: datetime) -> dict[str, Any] | None:
        """The assessment in force at ``t``: the last tick at or before it, with the decision made
        then, its feature readings and the last level change before it."""
        rows = self.query("""SELECT as_of, risk_score, state, monitor_ready, degraded, degraded_reasons,
                             not_ready_reasons, panic_z, drift_z, volume_z, cusum_stat, cusum_alarm_active,
                             top_headline_ids FROM risk_scores WHERE run_id = ? AND as_of <= ?
                             ORDER BY as_of DESC, rowid DESC LIMIT 1""", [run_id, _ts(t)])
        if not rows:
            return None
        m = rows[0]
        for k in ("degraded_reasons", "not_ready_reasons", "top_headline_ids"):
            m[k] = json.loads(m[k] or "[]")
        at = _ts(datetime.fromisoformat(m["as_of"]))
        d = self.query("""SELECT prev_state, new_state, changed, rule, reason, changepoint FROM decisions
                          WHERE run_id = ? AND as_of = ? ORDER BY wall_ts DESC, rowid DESC LIMIT 1""", [run_id, at])
        m["decision"] = d[0] if d else None
        if m["decision"] and m["decision"]["changepoint"]:
            m["decision"]["changepoint"] = json.loads(m["decision"]["changepoint"])
        m["features"] = {r.pop("component"): r for r in self.query(
            """SELECT component, raw, z, sample_count, baseline_center, baseline_scale, reason FROM features
               WHERE run_id = ? AND as_of = ? ORDER BY rowid DESC""", [run_id, at])[::-1]}
        last = self.query("""SELECT as_of, prev_state, new_state, rule, reason FROM decisions
                             WHERE run_id = ? AND changed AND as_of <= ? ORDER BY as_of DESC, rowid DESC LIMIT 1""",
                          [run_id, at])
        m["last_change"] = last[0] if last else None
        return m

    def headlines_by_id(self, run_id: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        ph = ",".join(["?"] * len(ids))
        rows = self.query(f"""SELECT headline_id, headline, source, url, event_time, timestamp_quality,
                              p_negative, sentiment_impl, data_origin FROM headlines
                              WHERE run_id = ? AND headline_id IN ({ph})""", [run_id, *ids])
        order = {h: i for i, h in enumerate(ids)}
        return sorted(rows, key=lambda r: order.get(r["headline_id"], 1 << 30))

    def control_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM control_events WHERE run_id = ? ORDER BY ts DESC", [run_id])


def _feature_row(run_id: str, as_of: datetime, c: ComponentReading) -> tuple:
    return (run_id, _ts(as_of), c.name, c.available, c.raw, c.z, c.z_unclipped, c.sample_count,
            c.baseline_count, c.baseline_center, c.baseline_scale, c.baseline_ready, c.reason,
            c.implementation, c.is_fallback)
