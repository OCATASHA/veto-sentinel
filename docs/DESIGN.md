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

### 2b. "Optimize for": relevance weighting (`features/relevance.py`)

A whole-market monitor dilutes the news a desk cares about: a burst of negative oil headlines is
a few percent of all headlines, and a cluster of unrelated bad news moves the score as much. The
operator can set a **focus**, a few comma-separated phrases (dashboard header → *Optimize for*,
config `[focus] text`). Every headline then enters every feature with a weight

    w = b + (1 − b)·r,    r ∈ [0, 1] = relevance to the focus,    b = background_weight (default 0.05)

so off-focus news still counts a little and a market-wide shock is still seen.

**Relevance r.** With MiniLM: s = maxₖ cos(e, qₖ) between the headline vector and each phrase
vector; z = (s − m)/σ, where m and σ are the median and 1.4826·MAD of s over up to 5,000 past
headlines (σ ≥ 0.02); r = logistic((z − 3)/0.5). Most headlines are off-topic, so (m, σ) describe
"a typical headline" and a match must be about 3 robust spreads closer to the focus (r = 0.5;
r = 0.88 at 4, 0.98 at 5). The calibration uses only headlines already seen (causal), is fitted
once and then frozen, so a headline's weight never changes after the fact. A semantic focus set
before 300 headlines exist activates (and rebuilds its baselines) when that number is reached.
Measured on 6,000 live GDELT headlines (2026-10-24): for "OPEC, crude oil prices, oil supply,
refineries, oil tankers", m = 0.159, σ = 0.074, and 2.3% of headlines reach r ≥ 0.5. The top
matches are oil-price stories, and the r ≈ 0.17 tail starts with unrelated finance stories. With
only the hashing fallback embedder, hashed vectors collide too often to measure topic similarity
("billionaire tax" scored as banking news), so relevance falls back to labelled keyword matching
(`fallback-keyword-relevance-v1`): r = the largest fraction of a phrase's words found in the
headline, with words matching on a shared stem of ≥ 4 letters (banks ~ banking, but ban ≁ banks).

**Weighted statistics.** Treat w as the probability that a headline belongs to the focus
(Poisson thinning):
- P = Σ aᵢwᵢpᵢ / Σ aᵢwᵢ (a = source weight).
- D uses weighted centroids. Since E‖Σwᵢvᵢ‖² = W₂ + (W² − W₂)‖μ‖² for W = Σw, W₂ = Σw², the
  unbiased norm is (‖S‖² − W₂)/(W² − W₂) and the cross term divides by W_rec·W_ref. At w = 1
  these are exactly the unweighted formulas
  (`tests/test_relevance.py::test_weighted_drift_bias_correction_is_unbiased`).
- V counts C = Σcᵢwᵢ (c = count weight). A thinned Poisson count is Poisson, so the scale floor
  1/√(1 + median C) still applies.
- Minimum-sample checks use the effective count n_eff = W²/W₂ (the plain count at w = 1).

**Baselines are rebuilt.** A focus change re-weights every retained headline (panic rows are
kept for the 7-day baseline period, vectors for the reference window, counts for 28 days) and
recomputes the P and D z-score history over the last 7 days on a 10-minute grid, so the current
reading and its "normal" use the same weights. The focused P rests on fewer effective headlines
and is noisier; its baseline spread widens to match, so z stays comparable. The change is logged
(`FOCUS_SET` control event), broadcast in every bus message (`focus`), and the monitor
re-assesses immediately: escalation is immediate, and relaxing still needs the usual calm period.

**Effect on sensitivity.** For a weighted mean, a shift d in a subset A of the headlines gives
z = d·Σ_A w / (σ·√Σw²). Relative to the whole-market setting, news about the focus therefore
weighs √(n/Σw²) times as much and off-focus news b·√(n/Σw²) times as much. The dashboard shows
both, computed from the last day's headlines. In the pipeline simulation
(`test_focus_amplifies_relevant_news_and_damps_the_rest`: 39 headlines/hour, 3 about oil,
b = 0.05) the formula predicts ×3.4 for oil news and ÷5.7 for other news; the measured panic
z-score response was ×3.4 (+1.86 → +6.33) to an oil shock and ÷5.6 (+0.90 → +0.16) to a
football shock.

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
`S > h` (k = 1, h = 6), then reset. It is updated once per 5 minutes of news time (every replay
tick). Live news time stands still between GDELT batches and then jumps, so the monitor adds the
current reading once per 5-minute step crossed (at most an hour's worth), not once per
60-second tick. The alarm stays active for 60 minutes. Missing R skips the
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
   The condition must hold continuously for 30 minutes, and any interruption resets the timer
   (`cooldown_pending`). The 30 minutes are simulated time in a replay and real time live: live
   news arrives late and in 15-minute batches, so timing it in news time would make the wait
   unpredictable. It also only relaxes once new news has been assessed during the wait (live: new
   headlines arrived), so a stalled feed is never counted as calm.
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
