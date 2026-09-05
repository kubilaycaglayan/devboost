#!/usr/bin/env bash
# DevBoost dev server — always runs the LATEST repo code in the foreground.
#
# Unlike `devboost serve` (which runs the installed copy under
# ~/.config/devboost), this script execs the devboost.py sitting next to it,
# so Rosa edits go live with a simple rerun. No build step: Python reads the
# file fresh on every start.
#
# - Rerunning takes the port: anything already listening on the target port
#   (e.g. a previous dev server) is stopped first. If the holder is our own
#   background dashboard, it is unloaded via launchctl and automatically
#   reloaded when this dev server exits — so the port is always free for the
#   new instance and the system returns to its prior state afterwards.
# - State (servers, syncs, forwards) is shared with the installed app, so you
#   see your real data (falls back to ./app when there is no installed state).
# - Default port is 3080, the standard dashboard port: the dev server stops
#   the existing operation and takes over the SAME port (no new port).
#   Override with: ./serve-dev.sh --port 3090
# - Sync agents created from this dashboard point at the repo copy too, so
#   background syncs also pick up latest code on their next run.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LIVE_APP_DIR="$HOME/.config/devboost/app"
if [ -d "$LIVE_APP_DIR" ]; then
  export DEVBOOST_APP_DIR="$LIVE_APP_DIR"
fi

agent_domain() {
  local domain=""
  if [ -f "$SCRIPT_DIR/app/.env" ]; then
    domain=$(grep -E '^(DEVBOOST_AGENT_DOMAIN|PORT_TRACKER_AGENT_DOMAIN)=' "$SCRIPT_DIR/app/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "'\"")
  fi
  echo "${domain:-com.user.devboost}"
}

AGENT_LABEL="$(agent_domain).dashboard"
AGENT_PLIST="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
UNLOADED_AGENT=""

restore_agent() {
  if [ -n "$UNLOADED_AGENT" ]; then
    echo "Restoring background dashboard..."
    launchctl load -w "$UNLOADED_AGENT" 2>/dev/null || true
    UNLOADED_AGENT=""
  fi
}

# Installed FIRST — before any state change below — so even a signal landing
# mid-takeover (e.g. timeout/Ctrl-C during free_port) still restores an
# unloaded agent instead of leaving it disabled.
stop_child() {
  # Stop the server child so it never outlives this script as an orphan
  # (e.g. when `timeout` signals only us, not the process group).
  if [ -n "${CHILD:-}" ]; then
    kill "$CHILD" 2>/dev/null || true
  fi
}
trap restore_agent EXIT
trap 'stop_child; exit 130' INT
trap 'stop_child; exit 143' TERM

port_listeners() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

# Gracefully stands down our own background dashboard when it holds the port
# (killing its PID is pointless — KeepAlive just restarts it). Sets
# UNLOADED_AGENT so the EXIT trap reloads it. Returns 0 when the port is free.
try_dashboard_unload() {
  local port="$1" pids="$2" pid
  if [ -z "$pids" ] || [ -n "$UNLOADED_AGENT" ]; then
    return 1
  fi
  # Only touch it when EVERY holder looks like our dashboard server.
  for pid in $pids; do
    if ! ps -o args= -p "$pid" 2>/dev/null | grep -q "devboost\.py.*serve"; then
      return 1
    fi
  done
  if ! launchctl list 2>/dev/null | grep -q "$AGENT_LABEL"; then
    return 1
  fi
  echo "Port $port is held by the background dashboard — unloading it so the dev server can start..."
  echo "(it will be reloaded automatically when this dev server exits)"
  launchctl unload -w "$AGENT_PLIST" 2>/dev/null || true
  UNLOADED_AGENT="$AGENT_PLIST"
  sleep 2
  if [ -z "$(port_listeners "$port")" ]; then
    return 0
  fi
  return 1
}

# Frees a TCP port so the new server can bind it. Returns 0 when free.
free_port() {
  local port="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  local pids
  pids=$(port_listeners "$port")
  if [ -z "$pids" ]; then
    return 0
  fi
  # Clean path first: our own dashboard stands down gracefully.
  if try_dashboard_unload "$port" "$pids"; then
    return 0
  fi
  # Generic path for anything else: stop it so the new server can bind.
  pids=$(port_listeners "$port")
  if [ -z "$pids" ]; then
    return 0
  fi
  local pid desc
  for pid in $pids; do
    desc=$(ps -o comm= -p "$pid" 2>/dev/null || echo "pid $pid")
    echo "Port $port is in use by $desc (pid $pid) — stopping it..."
    kill "$pid" 2>/dev/null || true
  done
  sleep 2
  pids=$(port_listeners "$port")
  if [ -z "$pids" ]; then
    return 0
  fi
  echo "Forcing (${pids})..."
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
  sleep 1
  pids=$(port_listeners "$port")
  if [ -z "$pids" ]; then
    return 0
  fi
  # A dashboard instance may have (re)started into the port meanwhile.
  if try_dashboard_unload "$port" "$pids"; then
    return 0
  fi
  echo "Port $port is still occupied (pids: $pids) — giving up."
  echo "Use another port:  ./serve-dev.sh --port 3090"
  return 1
}

# Resolve the effective port: CLI --port/-p wins, else env, else the standard
# dashboard port 3080 (take over the existing operation, don't open a new one).
PORT="${DEVBOOST_DASHBOARD_PORT:-3080}"
PREV=""
for a in "$@"; do
  if [ "$PREV" = "--port" ] || [ "$PREV" = "-p" ]; then
    PORT="$a"
  fi
  PREV="$a"
done

free_port "$PORT" || exit 1

NEED_PORT=1
for a in "$@"; do
  if [ "$a" = "--port" ] || [ "$a" = "-p" ]; then
    NEED_PORT=0
    break
  fi
done
if [ "$NEED_PORT" = 1 ]; then
  set -- --port "$PORT" "$@"
fi

# Run as a child (not exec) so the EXIT trap above can restore an unloaded agent.
# -u: unbuffered output, so startup/errors show immediately when piped.
/usr/bin/python3 -u "$SCRIPT_DIR/devboost.py" serve "$@" &
CHILD=$!
wait "$CHILD"
