#!/usr/bin/env bash
# Build, install, and launch DevBoost on the first paired physical iPhone.
#
# The installer updates the existing com.personal.devboost app in place. It
# never uninstalls the app, so the app's on-device data is retained.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SOURCE_DIR/scripts/mobile_test.py" --no-notification "$@"
