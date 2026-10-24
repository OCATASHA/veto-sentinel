# Veto Sentinel design

All numeric values below are the shipped defaults. They are **unvalidated starting
parameters** and every one is configurable in `config/default.toml`.

## 1. Ingestion and data contract

Adapters implement `fetch(now) -> FetchResult` and never raise for provider problems.
`AdapterRunner` adds the per-adapter poll interval, exponential backoff (honouring
`Retry-After`) and health tracking. An adapter exception is caught and recorded as a failure.

| Adapter | Source | Notes |
|---|---|---|
| `gdelt` | GDELT 2.0 DOC API (`mode=artlist`) | no key; `seendate` is GDELT's first-seen time, tagged `provider_seen`; non-JSON 200 bodies treated as errors; rolling ~3-month search window |
| `rss` | RSS 2.0 / Atom (Fed monetary + banking-regulation, ECB, BoE configured) | parsed with `defusedxml`; entity-expansion attacks rejected |
| `newsapi` | NewsAPI-compatible endpoint | only active if `VETOSENTINEL_NEWSAPI_KEY` is set; the key never appears in logs |
| `fixture` | JSONL with a mandatory `_meta` header | replay releases items whose time ≤ simulated now; a fixture may not claim `live_provider` |

Each item is normalised into a Pydantic `Headline`:
`headline_id` (`u:` + hash of the canonical URL, otherwise `t:` + text fingerprint), `headline`,
`source`, `url`, `canonical_url`, `text_fingerprint`, `published_at` (UTC or null),
`fetched_at` (UTC), `event_time`, `timestamp_quality`, `language`, `source_weight`,
`provider`, `data_origin`.

**Timestamps.** `published_at` and `fetched_at` are stored separately. `event_time` drives
all windows: it is `published_at`, except when that is missing or unparseable
(`missing`/`unparseable`) or more than 5 minutes in the future (`future_clamped`). In those
cases `fetched_at` is used and the original value is kept for audit. Naive timestamps are
assumed UTC and tagged `assumed_utc`.

**Dedup.** An item is a duplicate if its canonical URL or its text fingerprint was seen
within 7 days. Canonical URLs drop tracking parameters, `www.` and fragments. Fingerprints are
NFKC, lower-cased, with any trailing " - Source" syndication suffix removed. Syndicated copies
therefore count once, on purpose. Duplicates go to the `duplicates` table.

**Languages.** FinBERT and MiniLM are English models, so only `en`/`und` items are scored by
default. Other languages are counted as `skipped_language`.

## 2. Features: sign convention is *higher = more risk*

| Feature | Definition | Unavailable when |
|---|---|---|
| **P(t)** panic | Σ wᵢ·p_neg,ᵢ / Σ wᵢ over unique headlines in (t−60 min, t]; p_neg from FinBERT, with the negative class found from `id2label` (never assumed) | < 3 headlines in the window |
| **D(t)** drift | 1 − cos(centroid of (t−1 h, t], centroid of (t−7 d, t−1 h]) using unit-normalised MiniLM embeddings, with finite-sample bias correction of the centroid norms | < 5 recent or < 50 reference vectors, or a degenerate centroid |
| **V(t)** intensity | log1p(unique count in (t−60 min, t]) vs the same local time of day on previous days of the same type (weekday/weekend), up to 28 days back | < 3 matching history days, or the current window not fully covered by ingestion |

**Drift bias correction.** For n unit vectors with sum S, ‖S/n‖² overestimates ‖μ‖² by
(1−‖μ‖²)/n. Plain centroid distance therefore *shrinks* when news volume rises. On the
synthetic stress scenario that hid the topic shift completely (stress median D was below
calm-period noise). D uses the unbiased estimate (‖S‖² − n)/(n(n−1)) for each norm, so it
is still a cosine distance, but its expectation no longer depends on window size
(`tests/test_dashboard_and_tools.py::test_drift_bias_correction_removes_sample_size_effect`).
As an estimate it can be slightly negative. It is bounded to [−1, 2] and not clipped at 0,
because clipping would collapse the robust scale. Set `drift.bias_correction = false` for
the plain definition.

**Intensity time handling.** Everything is stored in UTC. `baseline_timezone` (default
`UTC`) only decides which earlier instants count as "the same time of day". With
`America/New_York`, the baseline follows US hours across DST changes, and nonexistent or
ambiguous local times resolve with `fold=0`. Live coverage starts at the first successful
fetch, not at the oldest backfilled item: an RSS feed's last 15 items do not make earlier
days look quiet. Ingestion outages are recorded as gaps and excluded from baselines.

## 3. Normalisation and composite

P and D are z-scored against their own history in [t−7 d, t−1 h]. The 1-hour gap stops an
emerging shock from absorbing itself into "normal". V uses its time-of-day baseline.

- robust centre/scale: median and 1.4826·MAD (`method = "standard"` gives mean/std)
- scale floor: `min_scale` per feature, plus a Poisson noise floor for V (1/√(1+median count))
- fewer than 48 baseline samples → baseline not ready → component unavailable
- z clipped to ±10, then floored at `z_floor = 0`, so calm readings cannot offset stressed ones
- NaN/inf are rejected at every boundary (Pydantic validators, tracker inserts, JSON encoding)

`R(t) = 0.4·z(P) + 0.3·z(D) + 0.3·z(V)`. Weights are validated: non-negative, exactly these
three keys, summing to 1.

**Degraded-data policy.** Nothing is silently set to zero.
- A non-required component is unavailable → it contributes nothing, the snapshot is
  `degraded` with the reason, and the state machine floors the state at `CAUTION`
  (configurable to `HALT`). R is then a lower bound and is labelled as such.
- A required component (`required_for_ready`, default `volume`) is unavailable, no component
  is available, or ingestion is stale (no successful fetch for 15 minutes) → the monitor is
  **not ready** → `HALT`.

Every tick stores, per component: raw value, z, unclipped z, sample count, baseline count /
centre / scale / readiness, reason, implementation id and `is_fallback`.

## 4. Change detection

`detect/base.py` defines the `ChangeDetector` protocol (`update(t, x) -> ChangePointResult`).
The shipped detector is a one-sided CUSUM on R(t): `S = max(0, S + R − k)`, alarm when
`S > h` (k = 1, h = 6), then reset. The alarm stays active for 60 minutes. Missing R skips the
update and is never treated as 0. Assumptions (not validated): roughly independent ticks, and an
in-control mean of R below k. Overlapping windows break independence, so treat h as a
sensitivity knob, not a calibrated false-alarm rate. `ruptures` is not used. A Bayesian online
change-point detector could implement the same protocol.

## 5. State machine (`engine/state_machine.py`)

Evaluated once per tick, in order:

1. `operator_flatten_hold`: FLATTEN is sticky. Only `clear_flatten` (an operator action) leaves it, to HALT.
2. `monitor_not_ready`: go to HALT immediately.
3. Escalation target = max(score level, change-point level, degraded floor):
   - score: R ≥ 3.5 → HALT; R ≥ 2.0 → CAUTION
   - active CUSUM alarm → at least CAUTION, or HALT if R ≥ 2.0
   - degraded → at least CAUTION
   If the target is more severe than the current state, escalate immediately (`escalate`).
4. De-escalate one level per cooldown (`deescalate_after_cooldown`):
   HALT→CAUTION needs R < 2.5 and target ≤ CAUTION; CAUTION→NORMAL needs R < 1.0 and target = NORMAL.
   The condition must hold continuously for 30 minutes of data time, and any interruption resets
   the timer (`cooldown_pending`).
5. Otherwise `hold`.

The machine starts in HALT. Data time must not go backwards (that raises an error). Config
validation enforces exit < enter for both levels, caution_enter < halt_enter and
caution_exit ≤ halt_exit.

Each tick produces a `DecisionRecord`: as_of (data time), wall_ts, prev/new state, changed,
rule, a human-readable reason (thresholds, z-scores, CUSUM, degraded reasons), risk score,
component z-scores, change-point output, top headline ids, and health (implementations,
model errors, adapter health, readiness, degraded reasons).

## 6. Persistence (DuckDB, `store.py`)

Tables: `runs` (mode, data origin, fixture metadata, full config, implementations,
parameter status), `headlines` (with p_negative and model ids), `duplicates`, `features`,
`risk_scores`, `decisions`, `control_events`, `ingest_log`. Timestamps are naive UTC.
One process owns the database. The dashboard runs in that process and shares the
connection behind a lock.

## 7. Bus, heartbeat and clients

See [MESSAGE_SCHEMA.md](MESSAGE_SCHEMA.md).

## 8. Process model

`vetosentinel replay|live` runs four threads: the analytics loop (Monitor ticks), the publisher
(owns the PUB socket and sends heartbeats), the uvicorn dashboard, and optionally an in-process
Python SDK client so the dashboard can show what a subscriber sees. The analytics loop reports
liveness. If it is silent for `pipeline_stale_ms` (30 s), the publisher overrides outgoing
messages to `HALT, monitor_ready=false`.
