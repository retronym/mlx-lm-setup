#!/usr/bin/env bash
# The nightly search refresh as a per-user launchd agent (time from pipelines/search/config/search.json refresh.at, default 03:00).
# Runs pipelines/search/refresh.sh at low CPU and I/O priority; a run missed because the Mac was asleep starts when it wakes.
# The gateway service must be running: the refresh gets its embeddings, LLM and NLI model from it.
#   service/search-refresh.sh install | uninstall | run [refresh args] | status | logs | plist   (plist: write and print it, nothing is loaded)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd -P)"
LABEL=com.retronym.local-models-search-refresh
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG=.gateway/logs/search-refresh.out
AT="$(python3 -c "import json;print(json.load(open('pipelines/search/config/search.json'))['refresh']['at'])" 2>/dev/null || echo 03:00)"

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" .gateway/logs
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/pipelines/search/refresh.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$((10#${AT%%:*}))</integer><key>Minute</key><integer>$((10#${AT##*:}))</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/$LOG</string>
  <key>StandardErrorPath</key><string>$ROOT/$LOG</string>
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
</dict></plist>
PL
  plutil -lint "$PLIST" >/dev/null
}
loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

case "${1:-status}" in
  install)   write_plist; loaded && launchctl bootout "$DOMAIN/$LABEL" || true
             launchctl bootstrap "$DOMAIN" "$PLIST"; echo "installed: refresh every night at $AT ($PLIST)"
             curl -fsS --max-time 3 http://127.0.0.1:8090/healthz >/dev/null 2>&1 || echo "note: the gateway is not answering on :8090; the refresh needs it (service/service.sh install)";;
  uninstall) loaded && launchctl bootout "$DOMAIN/$LABEL" || true; rm -f "$PLIST"; echo uninstalled;;
  plist)     write_plist; cat "$PLIST";;
  run)       shift; exec pipelines/search/refresh.sh "$@";;
  status)    if loaded; then launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|runs|last exit code|next) =|StartCalendarInterval" || true; else echo "not installed"; fi
             python3 - <<'PY'
import json, time, os
p = "pipelines/search/data/refresh.json"
if os.path.exists(p):
    r = json.load(open(p)); lr = r.get("last_run") or {}
    print(f"last refresh: {'ok' if lr.get('ok') else 'FAILED'} {int((time.time() - lr.get('started', 0)) / 3600)} h ago ({lr.get('universe')}); " + ", ".join(f"{k} {'ok' if v['ok'] else 'FAILED'}" for k, v in (lr.get('phases') or {}).items()))
else:
    print("no refresh has run in this checkout yet")
PY
             ;;
  logs)      tail -n "${2:-60}" -F "$LOG";;
  *) echo "usage: $0 install|uninstall|run [args]|status|logs|plist" >&2; exit 1;;
esac
