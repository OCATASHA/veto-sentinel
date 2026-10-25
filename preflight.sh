#!/usr/bin/env bash
# Check everything a demo needs before the demo: AI models cached, datasets, C++ bot built,
# free ports, GDELT reachable, Gemini key, alert targets. Exit code 1 if something will break.
#
#   ./preflight.sh                     # all checks
#   ./preflight.sh --offline           # skip the network checks
#   ./preflight.sh --send-test-alert   # also send a test message to your phone/chat targets
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export HF_HUB_OFFLINE=1   # only look at the local model cache
exec uv run --offline vetosentinel preflight "$@"
