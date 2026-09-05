#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  # Export variables from .env ignoring comments
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Configuration defaults
CONFIG_DIR="${PORT_TRACKER_CONFIG_DIR/#\~/$HOME}"
CONFIG_DIR="${CONFIG_DIR:-"$HOME/.config/port-tracker"}"
LOG_DIR="${PORT_TRACKER_LOG_DIR/#\~/$HOME}"
LOG_DIR="${LOG_DIR:-"$HOME/Library/Logs"}"
AGENT_DOMAIN="${PORT_TRACKER_AGENT_DOMAIN:-"com.user.port-tracker"}"
DASHBOARD_PORT="${PORT_TRACKER_DASHBOARD_PORT:-3080}"

echo "Deploying Port Tracker..."
echo "  Source: $SCRIPT_DIR"
echo "  Destination: $CONFIG_DIR"
echo "  Dashboard Port: $DASHBOARD_PORT"

mkdir -p "$CONFIG_DIR" "$HOME/.local/bin" "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Copy python executable
cp "$SCRIPT_DIR/asus_ports.py" "$CONFIG_DIR/asus_ports.py"
chmod +x "$CONFIG_DIR/asus_ports.py"

# Copy .env to config directory if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  cp "$SCRIPT_DIR/.env" "$CONFIG_DIR/.env"
fi

# Symlink CLI command
ln -sf "$CONFIG_DIR/asus_ports.py" "$HOME/.local/bin/asus-ports"

# Render and install dashboard LaunchAgent
PLIST_TARGET="$HOME/Library/LaunchAgents/${AGENT_DOMAIN}.dashboard.plist"
if [ -f "$SCRIPT_DIR/launchagents/dashboard.plist.template" ]; then
  sed \
    -e "s|{{AGENT_DOMAIN}}|$AGENT_DOMAIN|g" \
    -e "s|{{CONFIG_DIR}}|$CONFIG_DIR|g" \
    -e "s|{{DASHBOARD_PORT}}|$DASHBOARD_PORT|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    "$SCRIPT_DIR/launchagents/dashboard.plist.template" > "$PLIST_TARGET"

  launchctl unload "$PLIST_TARGET" 2>/dev/null || true
  launchctl load -w "$PLIST_TARGET"
fi

echo "Port Tracker installed and activated successfully!"
echo "  CLI Command: asus-ports"
echo "  Web Dashboard: http://localhost:$DASHBOARD_PORT"
