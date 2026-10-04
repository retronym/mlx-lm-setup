#!/usr/bin/env bash
# Entry point launchd runs: rotate the gateway log (launchd never does), then exec the gateway with output appended to it.
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=.gateway/logs/gateway.out
KEEP=3
MAX=$((5 * 1024 * 1024))
mkdir -p .gateway/logs
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt "$MAX" ]; then
  for i in $(seq $((KEEP - 1)) -1 1); do [ -f "$LOG.$i" ] && mv "$LOG.$i" "$LOG.$((i + 1))"; done
  mv "$LOG" "$LOG.1"
fi
exec .venv/bin/python -m gateway >> "$LOG" 2>&1
