#!/usr/bin/env bash
# Replay REAL past headlines (GDELT archive) through Veto Sentinel, with the dashboard.
#
#   ./run_history.sh                     # default: SVB bank failure, Feb-Mar 2023 (real headlines)
#   ./run_history.sh --file data/historical/other.jsonl
#   ./run_history.sh --download          # (re)download the default SVB dataset (internet, ~10-15 min)
#   ./run_history.sh --fallback          # lighter: labelled fallback heuristics, no AI models
#   ./run_history.sh --port 8011         # use another dashboard port (default 8001)
#
# Runs offline once the dataset exists. Uses its own port and database, so it can run alongside
# ./run_dashboard.sh (demo) and ./run_live.sh (live). Stop with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"

FILE="data/historical/gdelt_gkg_svb_2023.jsonl"
PORT=8001
PUB_PORT=5557
DOWNLOAD=0
PASS=()

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file) FILE="${2:?--file needs a path}"; shift ;;
    --download) DOWNLOAD=1 ;;
    -p|--port) PORT="${2:?--port needs a number}"; shift ;;
    --pub-port) PUB_PORT="${2:?--pub-port needs a number}"; shift ;;
    -f|--fallback|--no-open) PASS+=("$1") ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [[ $DOWNLOAD -eq 1 || ( ! -f "$FILE" && "$FILE" == "data/historical/gdelt_gkg_svb_2023.jsonl" ) ]]; then
  if [[ $DOWNLOAD -eq 0 ]]; then
    echo "The SVB dataset isn't on this computer yet ($FILE)."
    read -r -p "Download it now from GDELT? Needs internet, ~10-15 minutes, ~2 GB transferred, 15 MB kept [y/N] " ok
    [[ "$ok" =~ ^[Yy] ]] || { echo "Cancelled."; exit 1; }
  fi
  mkdir -p data/historical
  uv run vetosentinel fetch-gdelt-gkg \
    --start 2023-02-27T00:00:00Z --end 2023-03-15T00:00:00Z --every-n 4 \
    --out data/historical/gdelt_gkg_svb_2023.jsonl \
    --event "svb_failure_window=2023-03-08T21:00:00Z..2023-03-14T00:00:00Z"
  FILE="data/historical/gdelt_gkg_svb_2023.jsonl"
fi

if [[ ! -f "$FILE" ]]; then
  echo "Dataset not found: $FILE" >&2
  echo "Build one with: uv run vetosentinel fetch-gdelt-gkg --start ... --end ... --out $FILE" >&2
  exit 1
fi

echo "Replaying REAL archived headlines from $FILE"
echo "Note: real news, but the replay clock is simulated (days pass in minutes)."
exec ./run_dashboard.sh ${PASS[@]+"${PASS[@]}"} --port "$PORT" --pub-port "$PUB_PORT" -- \
  --fixture "$FILE" --db data/history.duckdb
