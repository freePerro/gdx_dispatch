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
#   ./dev-up.sh --reload app   # also hot-reload backend edits (reload overlay)
#
# The stack is the one layout (docker-compose.yml) plus the dev overlay
# (docker-compose.dev.yml): build from source, dev ports, dev volumes.
set -euo pipefail
cd "$(dirname "$0")/../.."

SHA="$(git rev-parse --short HEAD)"
BT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

FILES=(-f gdx_dispatch/docker/docker-compose.yml -f gdx_dispatch/docker/docker-compose.dev.yml)
ARGS=()
for a in "$@"; do
  if [ "$a" = "--reload" ]; then
    FILES+=(-f gdx_dispatch/docker/docker-compose.reload.yml)
  else
    ARGS+=("$a")
  fi
done

# The dev overlay declares db/uploads `external`, so Compose aborts on a fresh
# clone until they exist. `docker volume create` is a no-op on an existing one.
docker volume create docker_db_data >/dev/null
docker volume create docker_gdx_uploads >/dev/null

APP_VERSION="$SHA" BUILD_TIME="$BT" GIT_SHA_SHORT="$SHA" \
  docker compose -p docker --env-file ./.env "${FILES[@]}" up -d "${ARGS[@]}"

echo "[dev-up] stack up at version ${SHA} (built ${BT})"
