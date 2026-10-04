#!/usr/bin/env bash
# Register the gateway with Claude Code (user scope: every project and worktree, CLI and desktop app) over HTTP, carrying the token that
# start_backend / stop_backend / set_backend_policy require. Safe to re-run. The gateway need not be running to register.
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
TOKEN="$($PY -m gateway token)"
URL="$($PY -m gateway url)"
for sc in local user; do claude mcp remove local-models --scope $sc >/dev/null 2>&1 || true; done
claude mcp add --transport http --scope user local-models "$URL" --header "Authorization: Bearer $TOKEN"
echo "registered local-models -> $URL  (restart Claude Code to load the tools)"
