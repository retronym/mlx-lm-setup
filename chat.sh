#!/usr/bin/env bash
# Serve chat.html on localhost and open it. Needs ./serve.sh running (port 8080).
# Usage: ./chat.sh [port]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8765}"
URL="http://127.0.0.1:$PORT/chat.html"

# Serve only chat.html's directory, localhost only.
(sleep 1 && open "$URL") &
exec python3 -m http.server "$PORT" --bind 127.0.0.1
