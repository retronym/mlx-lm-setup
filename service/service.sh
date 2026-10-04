#!/usr/bin/env bash
# Manage the gateway as a per-user launchd agent (starts at login, restarts if it crashes).
#   service/service.sh install | uninstall | restart | status | logs
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd -P)"
LABEL=com.retronym.local-models-gateway
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" .gateway/logs
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/service/run.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ExitTimeOut</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
EOF
  plutil -lint "$PLIST" >/dev/null
}

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

case "${1:-status}" in
  install)
    if [ -f .gateway/gateway.lock ] && ! loaded && lsof -t .gateway/gateway.lock >/dev/null 2>&1; then
      echo "a gateway is already running by hand (pid $(lsof -t .gateway/gateway.lock | head -1)); stop it first (kill -TERM), then re-run." >&2; exit 1
    fi
    write_plist
    loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "installed $PLIST"; sleep 3; "$0" status ;;
  uninstall)
    loaded && launchctl bootout "$DOMAIN/$LABEL" || true      # SIGTERM: the gateway stops every backend before exiting
    rm -f "$PLIST"; echo "uninstalled" ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"; sleep 3; "$0" status ;;
  status)
    if loaded; then launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code|runs) =" || true; else echo "not installed/loaded"; fi
    curl -fsS --max-time 3 http://127.0.0.1:8090/healthz && echo || echo "gateway not answering on :8090" ;;
  logs)
    tail -n "${2:-50}" -F .gateway/logs/gateway.out ;;
  *) echo "usage: $0 install|uninstall|restart|status|logs" >&2; exit 1 ;;
esac
