#!/usr/bin/env bash
# Build the current checkout as a self-contained, per-user macOS app.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="${HOME:?HOME must be set}"
APP_TARGET="$USER_HOME/Applications/DevBoost.app"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/devboost-app.XXXXXX")"
STAGE_APP="$STAGE_DIR/DevBoost.app"

cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

command -v swiftc >/dev/null 2>&1 || {
  echo "Xcode Command Line Tools are required (swiftc was not found)." >&2
  exit 1
}

mkdir -p "$STAGE_APP/Contents/MacOS" "$STAGE_APP/Contents/Resources/bin" "$USER_HOME/Applications"
cp "$SOURCE_DIR/macos/Info.plist" "$STAGE_APP/Contents/Info.plist"
swiftc -parse-as-library -O -framework Cocoa "$SOURCE_DIR/macos/DevBoost.swift" -o "$STAGE_APP/Contents/MacOS/DevBoost"
cp "$SOURCE_DIR/devboost.py" "$STAGE_APP/Contents/Resources/devboost.py"
cp "$SOURCE_DIR/remote_transport.py" "$STAGE_APP/Contents/Resources/remote_transport.py"
cp "$SOURCE_DIR/docker_monitor.py" "$STAGE_APP/Contents/Resources/docker_monitor.py"
cp "$SOURCE_DIR/bin/DevBoost-dashboard" "$STAGE_APP/Contents/Resources/bin/DevBoost-dashboard"
cp "$SOURCE_DIR/bin/DevBoost-tunnel" "$STAGE_APP/Contents/Resources/bin/DevBoost-tunnel"
chmod 755 "$STAGE_APP/Contents/Resources/devboost.py" "$STAGE_APP/Contents/Resources/remote_transport.py" "$STAGE_APP/Contents/Resources/docker_monitor.py" "$STAGE_APP/Contents/Resources/bin/DevBoost-dashboard" "$STAGE_APP/Contents/Resources/bin/DevBoost-tunnel"
if [ -d "$SOURCE_DIR/assets" ]; then
  cp -R "$SOURCE_DIR/assets" "$STAGE_APP/Contents/Resources/assets"
fi
codesign --force --sign - "$STAGE_APP"

rm -rf "$APP_TARGET"
mv "$STAGE_APP" "$APP_TARGET"
echo "Built $APP_TARGET"
