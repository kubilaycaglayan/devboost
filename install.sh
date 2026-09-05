#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present (canonical app/.env first, legacy root .env fallback).
# Untracked per-checkout state lives in app/; executable stays in ~/.config (TCC-safe).
if [ -f "$SCRIPT_DIR/app/.env" ]; then
  # Export variables from .env ignoring comments
  export $(grep -v '^#' "$SCRIPT_DIR/app/.env" | xargs)
elif [ -f "$SCRIPT_DIR/.env" ]; then
  # Export variables from .env ignoring comments
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Configuration defaults (DEVBOOST_ canonical, PORT_TRACKER_ legacy fallback)
# CONFIG_DIR holds the executable (TCC-safe, outside ~/Documents).
# APP_DIR holds untracked runtime state (.env, config.json).
CONFIG_DIR="${DEVBOOST_CONFIG_DIR:-${PORT_TRACKER_CONFIG_DIR/#\~/$HOME}}"
CONFIG_DIR="${CONFIG_DIR/#\~/$HOME}"
CONFIG_DIR="${CONFIG_DIR:-"$HOME/.config/devboost"}"
APP_DIR_OVERRIDE="${DEVBOOST_APP_DIR:-${PORT_TRACKER_APP_DIR:-}}"
if [ -n "$APP_DIR_OVERRIDE" ]; then
  APP_DIR="${APP_DIR_OVERRIDE/#\~/$HOME}"
else
  APP_DIR="$CONFIG_DIR/app"
fi
REPO_APP_DIR="$SCRIPT_DIR/app"
LOG_DIR="${DEVBOOST_LOG_DIR:-${PORT_TRACKER_LOG_DIR/#\~/$HOME}}"
LOG_DIR="${LOG_DIR/#\~/$HOME}"
LOG_DIR="${LOG_DIR:-"$HOME/Library/Logs"}"
AGENT_DOMAIN="${DEVBOOST_AGENT_DOMAIN:-${PORT_TRACKER_AGENT_DOMAIN:-"com.user.devboost"}}"
AGENT_PREFIX="${DEVBOOST_AGENT_PREFIX:-${PORT_TRACKER_AGENT_PREFIX:-"com.user.devboost-forward"}}"
DASHBOARD_PORT="${DEVBOOST_DASHBOARD_PORT:-${PORT_TRACKER_DASHBOARD_PORT:-3080}}"

echo "Deploying DevBoost..."
echo "  Source: $SCRIPT_DIR"
echo "  Destination (executable): $CONFIG_DIR"
echo "  State dir: $APP_DIR"
echo "  Dashboard Port: $DASHBOARD_PORT"

mkdir -p "$CONFIG_DIR" "$APP_DIR" "$CONFIG_DIR/bin" "$HOME/.local/bin" "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Copy python executable
cp "$SCRIPT_DIR/devboost.py" "$CONFIG_DIR/devboost.py"
chmod +x "$CONFIG_DIR/devboost.py"

# Copy favicon assets (dashboard serves /favicon.png from here or falls back
# to the embedded copy inside devboost.py)
if [ -d "$SCRIPT_DIR/assets" ]; then
  mkdir -p "$CONFIG_DIR/assets"
  cp "$SCRIPT_DIR"/assets/* "$CONFIG_DIR/assets/" 2>/dev/null || true
fi

# Install scoped DevBoost wrappers so macOS Background Items shows
# DevBoost-dashboard / DevBoost-tunnel instead of python3 / ssh.
# Global binaries are untouched; only this app's agents use these paths.
if [ -f "$SCRIPT_DIR/bin/DevBoost-dashboard" ]; then
  cp "$SCRIPT_DIR/bin/DevBoost-dashboard" "$CONFIG_DIR/bin/DevBoost-dashboard"
  chmod +x "$CONFIG_DIR/bin/DevBoost-dashboard"
fi
if [ -f "$SCRIPT_DIR/bin/DevBoost-tunnel" ]; then
  cp "$SCRIPT_DIR/bin/DevBoost-tunnel" "$CONFIG_DIR/bin/DevBoost-tunnel"
  chmod +x "$CONFIG_DIR/bin/DevBoost-tunnel"
fi

# State: copy/migrate .env into APP_DIR (never overwrite a newer APP_DIR/.env
# with an older legacy file — explicit repo app/.env wins, then legacy root).
if [ -f "$REPO_APP_DIR/.env" ]; then
  cp "$REPO_APP_DIR/.env" "$APP_DIR/.env"
elif [ -f "$SCRIPT_DIR/.env" ]; then
  if [ ! -f "$APP_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env" "$APP_DIR/.env"
  fi
elif [ -f "$CONFIG_DIR/.env" ] && [ ! -f "$APP_DIR/.env" ]; then
  mv "$CONFIG_DIR/.env" "$APP_DIR/.env"
fi

# State: copy/migrate config.json into APP_DIR (same precedence, never clobber).
if [ -f "$REPO_APP_DIR/config.json" ]; then
  cp "$REPO_APP_DIR/config.json" "$APP_DIR/config.json"
elif [ -f "$SCRIPT_DIR/config.json" ]; then
  if [ ! -f "$APP_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.json" "$APP_DIR/config.json"
  fi
elif [ -f "$CONFIG_DIR/config.json" ] && [ ! -f "$APP_DIR/config.json" ]; then
  mv "$CONFIG_DIR/config.json" "$APP_DIR/config.json"
fi

# Symlink CLI command
ln -sf "$CONFIG_DIR/devboost.py" "$HOME/.local/bin/devboost"

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

# Migrate existing tunnel agents from raw /usr/bin/ssh to the scoped wrapper.
# Old BTM entries for "ssh" linger in the system database until the user
# removes them in Settings > General > Login Items or runs `sfltool resetbtm`,
# but all agents will now launch as DevBoost-tunnel going forward.
TUNNEL_WRAPPER="$CONFIG_DIR/bin/DevBoost-tunnel"
if [ -x "$TUNNEL_WRAPPER" ]; then
  python3 - "$HOME/Library/LaunchAgents" "${AGENT_PREFIX}" "$TUNNEL_WRAPPER" <<'PYEOF'
import glob
import os
import plistlib
import subprocess
import sys

launch_dir, prefix, wrapper = sys.argv[1], sys.argv[2], sys.argv[3]
for plist_path in glob.glob(os.path.join(launch_dir, f"{prefix}-*.plist")):
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        args = data.get("ProgramArguments", [])
        if args and args[0] == "/usr/bin/ssh":
            args[0] = wrapper
            data["ProgramArguments"] = args
            subprocess.run(["launchctl", "unload", plist_path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(plist_path, "wb") as f:
                plistlib.dump(data, f)
            subprocess.run(["launchctl", "load", "-w", plist_path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  Migrated tunnel: {os.path.basename(plist_path)} -> DevBoost-tunnel")
    except Exception as e:
        print(f"  Skipped {plist_path}: {e}")
PYEOF
fi

echo "DevBoost installed and activated successfully!"
echo "  CLI Command: devboost"
echo "  Web Dashboard: http://localhost:$DASHBOARD_PORT"
