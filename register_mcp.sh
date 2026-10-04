#!/usr/bin/env bash
# Register the gateway with Claude Code (this project, local scope) over HTTP, carrying the token that
# start_backend / stop_backend / set_backend_policy require. Safe to re-run. The gateway need not be running to register.
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
TOKEN="$($PY -m gateway token)"
URL="$($PY -m gateway url)"
claude mcp remove local-models --scope local >/dev/null 2>&1 || true
claude mcp add --transport http --scope local local-models "$URL" --header "Authorization: Bearer $TOKEN"
echo "registered local-models -> $URL  (restart Claude Code to load the tools)"
