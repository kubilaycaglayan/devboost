#!/usr/bin/env bash
# The development entry point: migrate state, rebuild, take port 3080, and
# launch the packaged DevBoost app detached by default. Use --foreground when
# developing the launcher itself and wanting the app/backend output in-place.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="${HOME:?HOME must be set}"
STATE_DIR="$USER_HOME/Library/Application Support/DevBoost"
LEGACY_DIR="$USER_HOME/.config/devboost"
LEGACY_PLIST="$USER_HOME/Library/LaunchAgents/com.user.port-tracker.dashboard.plist"
APP_PATH="$USER_HOME/Applications/DevBoost.app"
PORT="${DEVBOOST_DASHBOARD_PORT:-3080}"
FOREGROUND=false

case "${1:-}" in
  "") ;;
  --foreground|-f) FOREGROUND=true ;;
  *) echo "Usage: $0 [--foreground]" >&2; exit 2 ;;
esac

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

merge_source_env_defaults() {
  # The packaged app loads state from STATE_DIR, while developers commonly
  # keep local secrets in the checkout root .env. Copy only missing variables
  # so explicit runtime settings continue to take precedence on later runs.
  python3 - "$SOURCE_DIR/.env" "$STATE_DIR/.env" <<'PY'
import os
import re
import sys
import tempfile

source_path, target_path = sys.argv[1:]
if not os.path.isfile(source_path):
    raise SystemExit(0)

def assignments(path):
    try:
        with open(path, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
        lines = []
    parsed = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            parsed.append((key, line))
    return lines, parsed

target_lines, target_entries = assignments(target_path)
present = {key for key, _ in target_entries}
_, source_entries = assignments(source_path)
missing = []
for key, line in source_entries:
    if key not in present:
        missing.append(line)
        present.add(key)
if not missing:
    raise SystemExit(0)

if target_lines and target_lines[-1].strip():
    target_lines.append("")
target_lines.extend(missing)
directory = os.path.dirname(target_path)
fd, temporary = tempfile.mkstemp(prefix=".devboost-env-", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(target_lines) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

mkdir -p "$STATE_DIR/logs"
copy_state_if_missing .env
copy_state_if_missing config.json
merge_source_env_defaults

# Stop the native status-item owner before replacing its app bundle. Killing
# only its dashboard child later can briefly overlap two Cocoa app instances
# with the same bundle identifier and leave an orphaned backend with no menu.
native_pids="$(pgrep -f -x "$APP_PATH/Contents/MacOS/DevBoost --port $PORT" 2>/dev/null || true)"
if [ -n "$native_pids" ]; then
  echo "Stopping existing DevBoost app process(es): $native_pids"
  kill $native_pids 2>/dev/null || true
  for _ in {1..10}; do
    pgrep -f -x "$APP_PATH/Contents/MacOS/DevBoost --port $PORT" >/dev/null 2>&1 || break
    sleep 0.2
  done
fi

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
# Remove only the retired installed code. Keep the legacy state/assets tree so
# an unrelated file placed in the app's config directory is never swept up.
rm -f "$LEGACY_DIR/devboost.py"
rm -rf "$LEGACY_DIR/bin"
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

APP_LOG="$STATE_DIR/logs/devboost-app.log"
APP_PID_FILE="$STATE_DIR/devboost-app.pid"
if "$FOREGROUND"; then
  exec "$APP_PATH/Contents/MacOS/DevBoost" --port "$PORT"
fi

open -n "$APP_PATH" --args --port "$PORT"
APP_PID=""
for _ in {1..20}; do
  APP_PID="$(pgrep -f -x "$APP_PATH/Contents/MacOS/DevBoost --port $PORT" 2>/dev/null | head -1 || true)"
  [ -n "$APP_PID" ] && break
  sleep 0.2
done
if [ -z "$APP_PID" ]; then
  echo "DevBoost native app did not start; check Console.app for launch diagnostics" >&2
  exit 1
fi
printf '%s\n' "$APP_PID" > "$APP_PID_FILE"
for _ in {1..10}; do
  ! kill -0 "$APP_PID" 2>/dev/null && break
  lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
  sleep 0.2
done
if ! kill -0 "$APP_PID" 2>/dev/null; then
  echo "DevBoost native app exited during startup; see $APP_LOG" >&2
  exit 1
fi
echo "DevBoost relaunched detached (PID $APP_PID, port $PORT)."
echo "Logs: $APP_LOG"
