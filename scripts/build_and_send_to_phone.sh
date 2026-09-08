#!/usr/bin/env bash
# Build, install, launch, and notify using the same workflow used manually:
#
#   xcodebuild ... -destination platform=iOS,id=<paired-device-UDID> \
#     -allowProvisioningUpdates build
#   xcrun devicectl device install app --device <core-device-id> <DevBoost.app>
#   xcrun devicectl device process launch --device <core-device-id> com.personal.devboost
#   python3 scripts/mobile_test.py --no-build
#
# mobile_test.py discovers the paired device and computes the build path, so
# this wrapper never stores personal device identifiers in the repository.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/mobile_test.py" "$@"
