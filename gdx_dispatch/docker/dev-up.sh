#!/usr/bin/env bash
# Local dev: bring the stack up with a real version stamped from git, so
# /pwa/version and the admin version badge show the current commit instead of
# "dev"/"unknown". (Production gets these from the release workflow's build args.)
#
#   ./dev-up.sh            # whole stack
#   ./dev-up.sh app        # just one service
#   ./dev-up.sh --build     # forward any `docker compose up` flags
set -euo pipefail
cd "$(dirname "$0")/../.."

SHA="$(git rev-parse --short HEAD)"
BT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

APP_VERSION="$SHA" BUILD_TIME="$BT" GIT_SHA_SHORT="$SHA" \
  docker compose -p docker --env-file ./.env \
    -f gdx_dispatch/docker/docker-compose.yml up -d "$@"

echo "[dev-up] stack up at version ${SHA} (built ${BT})"
