#!/usr/bin/env bash
# Run Veto Sentinel on LIVE real-world news (central-bank feeds, GDELT, optional NewsAPI).
#
#   ./run_live.sh                 # needs internet
#   ./run_live.sh --fallback      # lighter: labelled fallback heuristics, no AI models
#   ./run_live.sh --port 8012     # use another dashboard port (default 8002)
#   ./run_live.sh --no-open       # don't open the browser
#
# News: GDELT's 15-minute raw files (global coverage) + central-bank RSS (+ NewsAPI if
# VETOSENTINEL_NEWSAPI_KEY is set). On start it loads the last 15 days from the GDELT archive so
# it can judge immediately. The FIRST start downloads ~1-2 GB and scores the headlines (several
# minutes); later starts reuse the cache in data/ and only fetch what's new.
# Stop with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"
# Optional local settings and secrets, never committed (git-ignored): e.g. GEMINI_API_KEY=...,
# VETOSENTINEL_NTFY_URL=https://ntfy.sh/<long random topic>
if [[ -f .env ]]; then set -a; source .env; set +a; fi

PORT=8002
PUB_PORT=5558
FALLBACK=0
OPEN_BROWSER=1

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--fallback) FALLBACK=1 ;;
    -p|--port) PORT="${2:?--port needs a number}"; shift ;;
    --pub-port) PUB_PORT="${2:?--pub-port needs a number}"; shift ;;
    --no-open) OPEN_BROWSER=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed: https://docs.astral.sh/uv/ (brew install uv)" >&2
  exit 1
fi
if [[ ! -x .venv/bin/vetosentinel ]]; then
  echo "First run: installing dependencies with uv..."
  if [[ $FALLBACK -eq 1 ]]; then uv sync; else uv sync --extra ml; fi
fi

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if port_busy "$PORT"; then echo "Port $PORT is in use. Try: $0 --port $((PORT + 1))" >&2; exit 1; fi
if port_busy "$PUB_PORT"; then echo "Port $PUB_PORT is in use. Try: $0 --pub-port $((PUB_PORT + 1))" >&2; exit 1; fi

if ! curl -fsS -m 8 -o /dev/null https://www.ecb.europa.eu/rss/press.html 2>/dev/null; then
  echo "Warning: can't reach the internet. Live mode needs it; the monitor will stay at \"Stop\" until news arrives." >&2
fi
echo "Probing news sources..."
uv run --offline vetosentinel probe-adapters --config config/default.toml --fallback-models 2>/dev/null \
  | grep -E '^(OK|ERR)' || true

URL="http://127.0.0.1:${PORT}"
ARGS=(live --config config/default.toml --port "$PORT" --pub "tcp://127.0.0.1:${PUB_PORT}" --db data/live.duckdb)
[[ $FALLBACK -eq 1 ]] && ARGS+=(--fallback-models)

if [[ $OPEN_BROWSER -eq 1 ]]; then
  (
    for _ in $(seq 1 1800); do
      kill -0 $$ 2>/dev/null || exit 0   # server gone: stop waiting
      if curl -fs -o /dev/null "${URL}/api/health"; then
        if command -v open >/dev/null 2>&1; then open "$URL"; else xdg-open "$URL" >/dev/null 2>&1 || true; fi
        exit 0
      fi
      sleep 1
    done
  ) &
fi

echo "Starting Veto Sentinel LIVE (signal bus tcp://127.0.0.1:${PUB_PORT}; Ctrl+C to stop)"
echo "Loading recent history first; the dashboard opens at ${URL} when that's done."
export HF_HUB_OFFLINE=1   # models come from the local cache; the news itself is live
exec uv run --offline vetosentinel "${ARGS[@]}"
