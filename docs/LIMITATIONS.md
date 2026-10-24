# Known limitations

This is a hackathon prototype. Each item below is a known gap, not a hidden one.

## Scope and claims
- No claim of production reliability, latency leadership, profitability, or ability to predict crises.
- All thresholds, weights, windows and minimum sample counts are **unvalidated starting
  parameters**. They were chosen for a conservative demo and adjusted only while developing on the
  *synthetic* fixture (the drift bias correction and the Poisson volume floor). They were not tuned
  on the historical SVB replay. One historical event is an anecdote, not a validation.
- The synthetic fixture is generated from templates by `scripts/generate_synthetic_fixture.py`.
  Detecting its injected scenarios shows that the mechanics work and nothing more.

## Data
- **GDELT DOC search API** refused our multi-term query with HTTP 429 on every probe (2026-10-24),
  even with long waits; a one-word query worked. It is therefore disabled. Live GDELT news comes from
  the 15-minute GKG raw files instead (no query limits), which was verified working live.
- **GDELT publishes with a lag.** Files are announced 30-60 minutes before they can be downloaded,
  and some are skipped entirely (the 19:00 UTC file on 2026-10-24 returned 404). Late files are
  retried for up to 3 hours. Live mode assesses the news *as of the time its data is complete*,
  typically 30-60 minutes behind real time, and is not ready if that lag exceeds 2 hours.
- **Federal Reserve** `press_all.xml`, although listed on federalreserve.gov/feeds, returned 404 when
  probed. The config uses the monetary-policy and banking-regulation feeds instead (both returned 200).
  ECB and BoE RSS returned 200. Feed availability and terms can change at any time.
- Central-bank RSS feeds have low volume (tens of items, mostly older). Without GDELT (or a news
  API key) the monitor would mostly sit at CAUTION or HALT. That's deliberate: sparse news is not
  evidence of calm.
- **Live warm start depends on the GDELT archive.** At startup, live mode loads the last 15 days
  from GDELT (hourly samples, weighted to match live 15-minute files) and replays them so it is
  ready at once. The first start downloads about 1-2 GB and scores ~40k headlines (minutes); later
  starts use the caches in `data/` and take seconds. If GDELT is unreachable at startup, it falls
  back to learning from scratch (days). Each fresh start still begins at "Stop" and relaxes one
  level per 30 calm minutes.
- GDELT GKG timestamps are 15-minute capture-batch times, not publication times. `--every-n 4`
  sampling keeps one batch per hour. Theme tagging and coverage are GDELT's, and page titles can be
  missing or noisy.
- Weekend baselines need three prior weekend days, so the first two weekends of any run are NOT READY.
- Only English (`en`/`und`) headlines are scored. Language detection is not performed; provider
  language tags are trusted.
- Dedup is exact on canonical URL or normalised-text fingerprint. Near-duplicate rewrites of the same
  wire story still count separately.
- Source weights are a static config map. There is no reliability model.

## Models
- FinBERT is used as published (`ProsusAI/finbert`). Its calibration on 2020s headlines, and on
  GDELT page titles in particular, has not been evaluated here.
- The fallback heuristics (`fallback-lexicon-v1`, `fallback-hashing-v1-*`) are crude. They exist only
  so the system stays runnable and visibly labelled without model weights.
- The drift bias correction makes D unbiased in expectation but noisy for small windows. D is an
  estimate and can be slightly negative.

## Statistics
- CUSUM assumes roughly independent observations. Overlapping rolling windows violate this, so `h`
  is not a calibrated false-alarm rate.
- Robust z-scores against a 7-day baseline adapt: a long crisis gradually becomes "normal". The
  1-hour baseline gap only delays this.
- R(t) is a weighted sum of z-scores floored at 0, not a probability.

## Transport and clients
- ZeroMQ PUB/SUB does not guarantee delivery. The heartbeat and watchdog make loss fail safe, but not
  reliable.
- There is no message authentication or encryption (ZeroMQ CURVE is not configured). Any process that
  can reach the port could publish. Bind to loopback or a trusted network only.
- Freshness checks compare publisher and client wall clocks, so they need NTP-synchronised hosts.
- Hooks run on the SDK's background threads. A slow hook delays processing of later messages, but the
  hot-path heartbeat-age check still forces HALT.
- The dashboard login is demo-grade. There is one shared account, and the built-in demo password
  is short (override it with `VETOSENTINEL_LOGIN_PASSWORD`). Sessions live in memory, so a restart
  logs everyone out. Rate limiting is per client address, and behind a reverse proxy all visitors
  share one address. All logged-in users share the same replay and controls. If
  `VETOSENTINEL_OPERATOR_TOKEN` is set, control endpoints also need `X-Operator-Token`, which the
  page does not send, so the controls become read-only. The server binds to 127.0.0.1 by default.

## Engineering
- Docker images and compose were written but **not built or run**: Docker was not installed in the
  development environment.
- The C++ SDK was built and tested with Apple clang 21 (libc++) on macOS arm64 only. The Debian
  Dockerfile targets g++ 12 and is untested.
- The DuckDB file is single-process. Run one monitor per database path.
- There is no market-data overlay on the dashboard (optional in the brief; not implemented).
