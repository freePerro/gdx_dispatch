#!/usr/bin/env bash
# Self-hosted GDX updater.
#
#   snapshot DB  ->  pull new image  ->  restart (entrypoint migrates on boot)
#
# Migrations run automatically inside the container (alembic upgrade head, idempotent).
# The one thing that protects you is the pre-update snapshot taken below — keep it.
#
# Usage:  ./update.sh            # pull whatever APP_VERSION in .env points at
#         APP_VERSION=1.3.0 ./update.sh   # pull a specific release
#
# Optional overrides (defaults match the shipped docker-compose, so the
# common case needs none of these):
#   HEALTH_PORT=8002 ./update.sh       # app published on a non-default host port
#   EXTRA_COMPOSE=path/to/override.yml ./update.sh   # layer an extra compose file
#                                                    # (e.g. a custom port mapping)
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (where .env lives)

COMPOSE=(docker compose -p gdx --env-file ./.env
  -f gdx_dispatch/docker/docker-compose.yml
  -f gdx_dispatch/docker/docker-compose.selfhost.yml)
# Layer an optional caller-supplied override (e.g. a non-default port mapping)
# onto the compose invocation. Empty by default → no change for standard setups.
[ -n "${EXTRA_COMPOSE:-}" ] && COMPOSE+=(-f "$EXTRA_COMPOSE")

ts="$(date +%Y%m%d-%H%M%S)"
snapshot="backups/gdx-pre-update-${ts}.sql.gz"
mkdir -p backups

echo "[update] Snapshotting database -> ${snapshot}"
"${COMPOSE[@]}" exec -T db pg_dump -U gdx gdx | gzip > "${snapshot}"

echo "[update] Pulling images…"
"${COMPOSE[@]}" pull app celery-high celery-low celery-beat

# Start app ALONE first: only app runs migrations (celery services skip them via
# the overlay), so the schema is migrated by exactly one container with no race.
echo "[update] Starting app (runs migrations)…"
"${COMPOSE[@]}" up -d db redis app

# Budget generously: app applies migrations BEFORE it serves /health, and a
# real DDL migration can take minutes. Default ~10 min; override with HEALTH_TIMEOUT.
tries="$(( ${HEALTH_TIMEOUT:-600} / 5 ))"
echo "[update] Waiting up to ${HEALTH_TIMEOUT:-600}s for health (migrations run first)…"
healthy=0
for _ in $(seq 1 "${tries}"); do
  if curl -sf "http://127.0.0.1:${HEALTH_PORT:-8001}/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "${healthy}" = "1" ]; then
  echo "[update] App healthy — starting workers…"
  "${COMPOSE[@]}" up -d
  echo "[update] Update complete."
  exit 0
fi

# Not healthy yet. Distinguish "still migrating" from "crashed" — restoring the
# snapshot while a migration is mid-flight is how you corrupt the DB.
status="$("${COMPOSE[@]}" ps -q app 2>/dev/null | xargs -r docker inspect -f '{{.State.Status}}' 2>/dev/null || true)"
echo "[update] App not healthy in time (container status: ${status:-unknown})." >&2
if [ "${status}" = "running" ] || [ "${status}" = "restarting" ]; then
  echo "[update] It may still be applying a long migration. DO NOT restore the snapshot yet." >&2
  echo "[update] Watch it finish:  ${COMPOSE[*]} logs -f app" >&2
  echo "[update] Workers were NOT started; rerun ./update.sh once /health is up." >&2
else
  echo "[update] App is not running — safe to roll back:" >&2
  echo "  1) pin the previous release in .env  (APP_VERSION=<old>)" >&2
  echo "  2) gunzip -c ${snapshot} | ${COMPOSE[*]} exec -T db psql -U gdx gdx" >&2
  echo "  3) ${COMPOSE[*]} up -d" >&2
fi
exit 1
