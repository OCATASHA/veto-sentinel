#!/usr/bin/env bash
# Run the Veto Sentinel dashboard locally (works offline) and open it in the browser.
#
#   ./run_dashboard.sh                 # FinBERT + MiniLM (from the local model cache)
#   ./run_dashboard.sh --fallback      # lighter: labelled fallback heuristics, no AI models
#   ./run_dashboard.sh --port 8001     # use another dashboard port
#   ./run_dashboard.sh -- --speed 600  # anything after -- goes to `vetosentinel replay`
#
# Stop with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
PUB_PORT=5556
FALLBACK=0
OPEN_BROWSER=1
EXTRA=()

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--fallback) FALLBACK=1 ;;
    -p|--port) PORT="${2:?--port needs a number}"; shift ;;
    --pub-port) PUB_PORT="${2:?--pub-port needs a number}"; shift ;;
    --no-open) OPEN_BROWSER=0 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed: https://docs.astral.sh/uv/ (brew install uv)" >&2
  exit 1
fi

# First run only: create the environment (needs internet once).
if [[ ! -x .venv/bin/vetosentinel ]]; then
  echo "First run: installing dependencies with uv (needs internet once)..."
  if [[ $FALLBACK -eq 1 ]]; then uv sync; else uv sync --extra ml; fi
fi

if [[ $FALLBACK -eq 0 ]] && ! .venv/bin/python -c \
    "import importlib.util as u, sys; sys.exit(0 if u.find_spec('transformers') and u.find_spec('sentence_transformers') else 1)"; then
  echo "Note: AI model packages not installed; the dashboard will use the labelled fallback heuristics."
  echo "      Install them with: uv sync --extra ml"
fi

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if port_busy "$PORT"; then
  echo "Port $PORT is already in use (another dashboard running?). Try: $0 --port $((PORT + 1))" >&2
  exit 1
fi
if port_busy "$PUB_PORT"; then
  echo "Signal-bus port $PUB_PORT is in use (another Veto Sentinel running?). Try: $0 --pub-port $((PUB_PORT + 1))" >&2
  exit 1
fi

URL="http://127.0.0.1:${PORT}"
ARGS=(replay --config config/default.toml --port "$PORT" --pub "tcp://127.0.0.1:${PUB_PORT}")
[[ $FALLBACK -eq 1 ]] && ARGS+=(--fallback-models)
ARGS+=(${EXTRA[@]+"${EXTRA[@]}"})

# Open the browser once the server answers (gives up after 90 s).
if [[ $OPEN_BROWSER -eq 1 ]]; then
  (
    for _ in $(seq 1 90); do
      kill -0 $$ 2>/dev/null || exit 0   # server gone: stop waiting
      if curl -fs -o /dev/null "${URL}/api/health"; then
        if command -v open >/dev/null 2>&1; then open "$URL"; else xdg-open "$URL" >/dev/null 2>&1 || true; fi
        exit 0
      fi
      sleep 1
    done
  ) &
fi

echo "Starting Veto Sentinel at ${URL}  (signal bus tcp://127.0.0.1:${PUB_PORT}; Ctrl+C to stop)"
echo "Welcome screen -> sign in -> console. Login: built-in demo account, or set"
echo "VETOSENTINEL_LOGIN_USER / VETOSENTINEL_LOGIN_PASSWORD to choose your own."
export HF_HUB_OFFLINE=1   # use cached models only; never contact Hugging Face
exec uv run --offline vetosentinel "${ARGS[@]}"
