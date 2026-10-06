#!/usr/bin/env bash
# A draft gateway for looking at the search page from THIS checkout (typically a worktree) while the real service keeps running from main.
# Serves only the search backend, on its own port (default 8091), with this checkout's index. Venvs come from the main checkout. `ask` takes its models
# (LLM, decision model) from the main gateway (GATEWAY_URL, default :8090).
#
#   pipelines/search/draft.sh start | stop | restart | status | logs  [port]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
ROOT="$(cd "$HERE/../.." && pwd -P)"
MAIN="$(cd "$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/.." && pwd -P)"     # the main checkout, which has the venvs
PORT="${2:-${DRAFT_PORT:-8091}}"
D="$ROOT/.draft"; CAT="$D/gateway.toml"; LOG="$D/gateway.log"
write_catalog() {
  mkdir -p "$D/state"
  cat > "$CAT" <<TOML
[gateway]
port = $PORT
backend_port_base = $((PORT + 10000))
memory_budget_gb = 28
state_dir = "$D/state"

[backends.scala-search]
adapter = "search"
python = "$MAIN/.venv-jev/bin/python"
index_dir = "$ROOT/pipelines/search"
aliases = ["search"]
est_mem_gb = 4
start_timeout_s = 240
ttl_s = 900
TOML
}
pid() { pgrep -f "gateway --catalog $CAT" || true; }
case "${1:-status}" in
  start)  [ -z "$(pid)" ] || { echo "already running (pid $(pid)) at http://127.0.0.1:$PORT/search"; exit 0; }
          write_catalog; (cd "$ROOT" && GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8090}" nohup "$MAIN/.venv/bin/python" -m gateway --catalog "$CAT" > "$LOG" 2>&1 < /dev/null &)
          sleep 4; echo "draft gateway: http://127.0.0.1:$PORT/search   (data: ${SEARCH_DATA_DIR:-$HERE/data}; the model starts on the first query)";;
  stop)   [ -z "$(pid)" ] || kill $(pid); echo stopped;;
  restart) "$0" stop "$PORT"; sleep 3; "$0" start "$PORT";;
  status) [ -n "$(pid)" ] && echo "running (pid $(pid)): http://127.0.0.1:$PORT/search" || echo "not running";;
  logs)   tail -n 50 "$LOG";;
  *) sed -n '2,6p' "$0";;
esac
