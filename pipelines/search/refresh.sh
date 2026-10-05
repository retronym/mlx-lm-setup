#!/usr/bin/env bash
# Run refresh.py with the right python and keep the Mac awake for the duration (caffeinate -i: no idle sleep while it runs).
# Venvs live in the main checkout, which is where this script normally runs from; from a worktree they are found through the git common dir.
#   pipelines/search/refresh.sh [refresh.py arguments]      e.g. --tier high, --dry-run, --budget-hours 2
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
ROOT="$(cd "$HERE/../.." && pwd -P)"
MAIN="$(cd "$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/.." && pwd -P)"
PY="$MAIN/.venv-jev/bin/python"
export GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8090}"      # embeddings, the LLM and the NLI model all come from the gateway
cd "$ROOT"
exec caffeinate -i "$PY" -u "$HERE/refresh.py" "$@"
