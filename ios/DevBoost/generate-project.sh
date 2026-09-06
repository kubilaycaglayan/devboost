#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$PROJECT_DIR/../.." && pwd)"
ENV_FILE="${DEVBOOST_ENV_FILE:-$REPO_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${IOS_DEVELOPMENT_TEAM:?Set IOS_DEVELOPMENT_TEAM in $ENV_FILE}"
command -v xcodegen >/dev/null 2>&1 || {
  echo "xcodegen is required to regenerate the iOS project." >&2
  exit 1
}

cd "$PROJECT_DIR"
xcodegen generate --spec project.yml
