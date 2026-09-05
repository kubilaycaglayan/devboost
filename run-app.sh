#!/usr/bin/env bash
# The only development entry point: migrate state, rebuild, take port 3080,
# and keep the packaged DevBoost app running until it is quit.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="${HOME:?HOME must be set}"
STATE_DIR="$USER_HOME/Library/Application Support/DevBoost"
LEGACY_DIR="$USER_HOME/.config/devboost"
LEGACY_PLIST="$USER_HOME/Library/LaunchAgents/com.user.port-tracker.dashboard.plist"
APP_PATH="$USER_HOME/Applications/DevBoost.app"
PORT="${DEVBOOST_DASHBOARD_PORT:-3080}"

copy_state_if_missing() {
  local name="$1" candidate
  local destination="$STATE_DIR/$name"
  [ -e "$destination" ] && return
  for candidate in "$LEGACY_DIR/app/$name" "$SOURCE_DIR/app/$name"; do
    if [ -f "$candidate" ]; then
      cp -p "$candidate" "$destination"
      return
    fi
  done
}

mkdir -p "$STATE_DIR/logs"
copy_state_if_missing .env
copy_state_if_missing config.json

"$SOURCE_DIR/build-app.sh"
"$APP_PATH/Contents/MacOS/DevBoost" refresh-agents

# Retire the legacy dashboard before taking its port. The packaged app is now
# the only dashboard runtime; persistent sync workers are recreated from state.
if [ -f "$LEGACY_PLIST" ]; then
  launchctl unload -w "$LEGACY_PLIST" 2>/dev/null || true
  rm -f "$LEGACY_PLIST"
fi

if [ -f "$STATE_DIR/config.json" ]; then
  for candidate in "$LEGACY_DIR/app/config.json" "$SOURCE_DIR/app/config.json"; do
    [ -f "$candidate" ] && cmp -s "$STATE_DIR/config.json" "$candidate" || true
  done
fi

# State has been copied before these exact legacy locations are removed.
rm -f "$SOURCE_DIR/app/.env" "$SOURCE_DIR/app/config.json"
rmdir "$SOURCE_DIR/app" 2>/dev/null || true
rm -rf "$LEGACY_DIR"
if [ -L "$USER_HOME/.local/bin/devboost" ]; then
  rm -f "$USER_HOME/.local/bin/devboost"
fi
find "$USER_HOME/Library/Logs" -maxdepth 1 -type f -name 'devboost-*.log' -delete

port_pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$port_pids" ]; then
  echo "Taking over port $PORT from process(es): $port_pids"
  kill $port_pids 2>/dev/null || true
fi
for _ in {1..10}; do
  lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 0.2
done
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  port_pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  echo "Force-stopping remaining port $PORT listener(s): $port_pids"
  kill -9 $port_pids 2>/dev/null || true
  sleep 0.2
fi
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is still occupied after takeover." >&2
  exit 1
fi

exec "$APP_PATH/Contents/MacOS/DevBoost" --port "$PORT"
