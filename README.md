# Veto Sentinel

**An independent, explainable, fail-safe news-based risk monitor for automated trading systems (hackathon prototype).**

Veto Sentinel watches financial-news headlines and central-bank communications, scores how
unusual and stressed the news narrative is, and publishes a graduated risk state
(`NORMAL` / `CAUTION` / `HALT` / operator-only `FLATTEN`) over ZeroMQ to strategy clients. The
clients fail safe: if the monitor is stale, silent, malformed, out of sequence or not ready,
they block new orders locally, without making a network call on the order path.

> Veto Sentinel is a **risk veto**, not a trading strategy, price predictor or liquidation
> engine. It is meant to make automated systems more cautious and observable. It does not
> claim to eliminate risk, predict crises, or improve profitability.

```
 news source / replay fixture
   │  GDELT DOC API · central-bank RSS (Fed, ECB, BoE) · optional NewsAPI · offline JSONL fixtures
   ▼
 normalise (UTC, provenance, canonical URL) ─► dedup (URL + text fingerprint) ─► DuckDB
   ▼
 features  P(t) panic      FinBERT p(negative), source-weighted       [fallback: lexicon heuristic]
           D(t) drift      MiniLM centroid cosine distance, 1h vs 7d  [fallback: hashing embedder]
           V(t) intensity  unique count vs same-time-of-day baseline
           optional "Optimize for": every headline weighted by relevance to your focus (MiniLM similarity)
   ▼
 rolling robust z-scores ─► R(t) = w_p·z(P) + w_d·z(D) + w_v·z(V)   (+ explicit degraded-data policy)
   ▼
 CUSUM change detector ─► state machine (thresholds, hysteresis, cooldown, readiness) ─► audit log
   ▼
 ZeroMQ PUB: sequenced STATE / HEARTBEAT / CONTROL messages, each a full snapshot (1 Hz heartbeat)
   ▼
 Python SDK · C++20 SDK: validate, sequence-check, watchdog ─► local atomic state ─► can_submit_order()
   ▼
 dashboard (live state, reasons, components, history, replay controls, operator FLATTEN, fault injection)
```

## Quick start

**SERVER SIDE WEBSITE ONGOING WORK**

For now use: (works offline once installed; opens the dashboard in your browser):

```bash
./run_dashboard.sh   # DEMO: invented news, fully offline            -> http://127.0.0.1:8000
./run_history.sh     # REAL past headlines (SVB failure, March 2023)  -> http://127.0.0.1:8001
./run_live.sh        # REAL live news (needs internet)                -> http://127.0.0.1:8002
```

Each script takes `--fallback` (no AI models), `--port N`, `--no-open` and `--help`. They use
separate ports and databases, so all three can run at once. `run_history.sh` offers to download
its dataset from GDELT if it's missing. `run_live.sh` loads the last 15 days of real news from the
GDELT archive on start, so it can judge immediately. The first start takes a few minutes and about
1-2 GB of downloads; later starts take seconds. Live news is assessed as of the time GDELT's data
is complete, usually 30-60 minutes behind real time.

You get an animated welcome screen, then a login page, then the console. The console has seven
sections (Overview, Warning signals, Timeline, Headlines, Trading bots, Controls, How it works),
written for non-experts. An **Expert details** switch reveals the technical numbers.

**Login.** The built-in demo account is `oxhack26` (the team has the password). The password is
stored only as a salted scrypt hash. To use your own credentials:

```bash
VETOSENTINEL_LOGIN_USER=alice VETOSENTINEL_LOGIN_PASSWORD='a long passphrase' ./run_dashboard.sh
```

Every data and control API route requires the session cookie (HttpOnly, SameSite=Strict).
Failed logins are rate-limited. This is demo-grade protection: before putting it on the
internet, set a strong password, serve over HTTPS, and ideally put an access proxy in front.

Requires [uv](https://docs.astral.sh/uv/). `.python-version` pins a native arm64 CPython 3.12
(on Apple Silicon an x86_64/Rosetta interpreter cannot use the torch wheels; on other
platforms, re-pin with `uv python pin 3.12`). Dependencies are locked in `uv.lock`.

```bash
uv sync                    # core + dev tools: runs with labelled fallback models
uv sync --extra ml         # optional: FinBERT + MiniLM (~1 GB with torch)
                           # (uv sync is exact: a later plain `uv sync` removes the ml extra again)

uv run pytest              # 119 tests (C++ interop test skips until built)

# Replay the SYNTHETIC demo fixture with publisher + dashboard + in-process demo client
uv run vetosentinel replay --config config/default.toml
#   -> http://127.0.0.1:8000   (use --fallback-models to skip FinBERT/MiniLM)

# In other terminals: external clients
uv run vetosentinel client-demo --endpoint tcp://127.0.0.1:5556
clients/cpp/build/strategy_demo tcp://127.0.0.1:5556      # after building, see below

# Reproducible headless evaluation (writes reports/<name>/*.md|json)
uv run vetosentinel evaluate --config config/default.toml --out reports/synthetic_finbert_minilm

# Live mode (public feeds; no keys required; NewsAPI only if VETOSENTINEL_NEWSAPI_KEY is set)
uv run vetosentinel probe-adapters --config config/default.toml
uv run vetosentinel live --config config/default.toml
```

C++ client (C++20, libzmq + cppzmq + nlohmann/json):

```bash
brew install zeromq cppzmq nlohmann-json cmake           # or: apt install libzmq3-dev cppzmq-dev nlohmann-json3-dev cmake
cmake -S clients/cpp -B clients/cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build clients/cpp/build -j && ctest --test-dir clients/cpp/build --output-on-failure
```

Docker (written, **not tested here**: Docker was not available in the development environment):

```bash
docker compose up --build        # monitor (synthetic replay, fallback models) + Python client + C++ client
```

## States

| State | Published when | Client default behaviour |
|---|---|---|
| `NORMAL` | R(t) below thresholds, monitor ready, no degraded data | normal order path |
| `CAUTION` | R ≥ `caution_enter`, active change-point, **or degraded data** | reduce-only orders, position limit × `caution_position_fraction` (`on_caution` hook) |
| `HALT` | R ≥ `halt_enter`, change-point with elevated R, **monitor not ready**, or any client-side fault | no orders; `on_halt` hook requests cancellation of resting orders |
| `FLATTEN` | **only** a two-step, human-confirmed operator instruction | no orders; `on_flatten` notifies a human. Nothing is liquidated automatically |

Escalation is immediate. De-escalation goes one level at a time, only after the exit
condition has held for `cooldown_minutes` (hysteresis: exit thresholds sit below enter
thresholds). Clients start in `HALT` and need `recovery_snapshots_required` (default 2)
consecutive valid, fresh, in-sequence snapshots before they adopt the published state.
See [docs/DESIGN.md](docs/DESIGN.md) and [docs/MESSAGE_SCHEMA.md](docs/MESSAGE_SCHEMA.md).

## Optimize for (relevance weighting)

By default every finance headline counts the same. In the dashboard, **Optimize for** (top bar)
takes a few comma-separated topics, such as "OPEC, crude oil supply, refineries, oil tankers", or
a preset. Each headline gets a relevance score: MiniLM similarity to the topics, compared with a
typical headline. Matching news counts fully and other news at 5% (adjustable), so a market-wide
crash still shows. The 7-day baselines are recomputed with the same weights, so the score still
compares like with like. The preview shows which recent headlines match and how much more (or
less) each kind of news moves the score. The change is logged and broadcast to clients
(`focus` field). It can also be set in config (`[focus] text`). The maths is in
[docs/DESIGN.md §2b](docs/DESIGN.md).

## Safety boundaries (enforced by design)

- No broker or exchange connectivity. No order submission. No automatic liquidation.
- `FLATTEN` needs an operator request, then a confirmation code from the same operator within
  2 minutes. Every step is written to `control_events`. Messages carry `"automatic_execution": false`.
- Market prices are **not** inputs to the score.
- Missing news, model failure, stale ingestion and warm-up never count as calm. They lead to
  `CAUTION` (a degraded component) or `HALT` (monitor not ready).
- Every fallback heuristic is labelled `fallback-*` in the stored features, the bus messages
  (`fallback_in_use`) and a dashboard banner.
- Synthetic data is labelled `data_origin: synthetic_fixture` in the fixture header, runs, messages,
  dashboard banner and every report.
- All numeric defaults are **unvalidated starting parameters** (`parameter_status` is stored with every run).

## Evaluation results

See [docs/EVALUATION.md](docs/EVALUATION.md). In short:

- **Synthetic demo fixture** (invented data; shows that the mechanics work, *not* real-world
  performance): both injected stress scenarios reached HALT within 25–45 minutes of onset. The
  3-hour feed outage was held at CAUTION, never NORMAL.
- **Historical GDELT replay around the March 2023 SVB failure** (49,820 real headlines, parameters
  fixed beforehand): the monitor did **not** give a timely news-driven signal. It stayed NORMAL through
  the 9 Mar share collapse (max R 0.27 in US hours). The first evidence-based CAUTION came 9.5 h
  after the chosen onset, and the first evidence-based HALT about 98 h after onset (the 12 Mar
  policy-response weekend). The fail-safes (data gap, warm-up) worked as designed. Likely cause:
  dilution across all finance headlines. Full analysis in the document.
- **Same replay, Optimize for "Banks & credit"** (preset written before the run, nothing tuned
  afterwards): the first evidence-based HALT moved from ~98 h to ~36 h after onset (Friday
  10 Mar, before SVB was closed), peak R rose from 2.35 to 3.78, and there were fewer alarms in
  the week before. There is still no signal on the 9 Mar share collapse itself. One event, not a
  validation.

## Repository layout

```
src/vetosentinel/
  config.py            typed config (Pydantic); defaults = unvalidated starting parameters
  models.py            Headline, ComponentReading, RiskSnapshot, DecisionRecord, enums
  ingest/              adapter interface + runner (backoff), GDELT, RSS/Atom, NewsAPI, fixture; normalise; dedup
  features/            backends (FinBERT/MiniLM + labelled fallbacks), panic, drift, intensity, normalise, composite, pipeline
  detect/              ChangeDetector protocol + CUSUM
  engine/              state machine (hysteresis/cooldown/FLATTEN) and Monitor orchestrator
  bus/                 versioned message schema, ZeroMQ publisher (heartbeat, self-watchdog, demo fault injection)
  client/              Python SDK: pure StateTracker + threaded VetoSentinelClient
  dashboard/           FastAPI JSON API + login (auth.py) + offline welcome/login and console pages
  evaluation/          replay evaluation report; GDELT GKG historical-fixture builder
  service.py runtime.py cli.py store.py
clients/cpp/           C++20 SDK (std::atomic state, subscriber + watchdog threads), tests, example
fixtures/              synthetic_demo.jsonl (SYNTHETIC, generated by scripts/generate_synthetic_fixture.py)
config/default.toml    all parameters, live adapters
reports/               generated evaluation reports
docs/                  DESIGN, MESSAGE_SCHEMA, EVALUATION, LIMITATIONS
```

Known limitations: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
