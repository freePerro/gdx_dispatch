#!/usr/bin/env bash
# Local dev: bring the stack up with a real version stamped from git, so
# /pwa/version, the admin version badge, AND the sidebar build stamp show the
# current commit instead of "dev"/"unknown".
#
# IMPORTANT: rebuild THROUGH this script, not `docker compose build`. The
# frontend bakes the git SHA into the bundle (__BUILD_SHA__) at image-build time
# from $GIT_SHA_SHORT; a bare `docker compose build` doesn't set it, so the
# sidebar shows "build unknown". `--build` below forwards the SHA as a build arg.
# (Production gets these from the release workflow's build args.)
#
#   ./dev-up.sh                # start whole stack (no rebuild)
#   ./dev-up.sh app            # start just one service
#   ./dev-up.sh --build app    # REBUILD app (bakes the SHA) then start
set -euo pipefail
cd "$(dirname "$0")/../.."

SHA="$(git rev-parse --short HEAD)"
BT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

APP_VERSION="$SHA" BUILD_TIME="$BT" GIT_SHA_SHORT="$SHA" \
  docker compose -p docker --env-file ./.env \
    -f gdx_dispatch/docker/docker-compose.yml up -d "$@"

echo "[dev-up] stack up at version ${SHA} (built ${BT})"
