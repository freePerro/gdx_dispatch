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
# Which compose files: COMPOSE_FILE from .env when it is set (production sets
# it to docker-compose.yml:docker-compose.prod.yml), else the one layout alone.
# The same line makes a bare `docker compose` in the repo root agree with this
# script, so the two can never run different stacks.
#
# Optional override:
#   EXTRA_COMPOSE=path/to/override.yml ./update.sh   # layer one more file
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (where .env lives)

# Compose strips one pair of surrounding quotes from a .env value; so does this.
files="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | tail -n 1 | sed -e "s/^\"\(.*\)\"\$/\1/" -e "s/^'\(.*\)'\$/\1/")"
files="${files:-gdx_dispatch/docker/docker-compose.yml}"
COMPOSE=(docker compose -p gdx --env-file ./.env)
IFS=: read -ra _files <<< "$files"
for f in "${_files[@]}"; do COMPOSE+=(-f "$f"); done
[ -n "${EXTRA_COMPOSE:-}" ] && COMPOSE+=(-f "$EXTRA_COMPOSE")

# An update runs over an existing database. If the files above resolve the
# database to a volume that does not exist yet, they are the wrong files (a
# missing COMPOSE_FILE line, a stale EXTRA_COMPOSE): `up` would create an empty
# volume, bootstrap would seed a fresh install and /health would go green
# while the real data sits detached. Stop before the snapshot touches anything.
db_volume="$("${COMPOSE[@]}" config --format json | tr -d ' \n' \
  | sed -n 's/.*"gdx_db_data":{"name":"\([^"]*\)".*/\1/p')"
if [ -z "${db_volume}" ] || ! docker volume inspect "${db_volume}" >/dev/null 2>&1; then
  echo "[update] Refusing: these compose files put the database on volume '${db_volume:-?}', which does not exist." >&2
  echo "[update] Files: ${files}${EXTRA_COMPOSE:+ + ${EXTRA_COMPOSE}}" >&2
  echo "[update] Set COMPOSE_FILE in .env to the files this install runs (docker volume ls shows the data volumes)." >&2
  echo "[update] A new install starts with docker compose -p gdx ... up -d (see README.md), not update.sh." >&2
  exit 1
fi

ts="$(date +%Y%m%d-%H%M%S)"
snapshot="backups/gdx-pre-update-${ts}.sql.gz"
mkdir -p backups

echo "[update] Snapshotting database -> ${snapshot}"
"${COMPOSE[@]}" exec -T db pg_dump -U gdx gdx | gzip > "${snapshot}"

echo "[update] Pulling images…"
"${COMPOSE[@]}" pull

# These compose files start the app under s6 (/init). An image from before the
# one-container layout has no /init: a checkout of main that pulls an older
# :latest, or one that pins an older APP_VERSION. Stop before anything is
# recreated, while the old containers still run.
# app's own image: `config --images app` also lists the services it depends on.
app_image="$("${COMPOSE[@]}" config app | awk '/^services:/ {s=1; next} /^[^ ]/ {s=0}
  s && /^  [^ ]/ {svc=$1} s && svc == "app:" && /^    image: / && !found {print $2; found=1}')"
init_rc=0
[ -n "${app_image}" ] && { docker run --rm --entrypoint test "${app_image}" -x /init || init_rc=$?; }
if [ -z "${app_image}" ] || [ "${init_rc}" != 0 ]; then
  if [ "${init_rc}" = 1 ]; then
    echo "[update] Refusing: ${app_image} has no /init, and these compose files start the app with it." >&2
    echo "[update] The image is older than this checkout. Pin a release at least as new as the checkout" >&2
    echo "[update] (APP_VERSION in .env), or check out the tag of the release you pinned (git checkout v<version>)." >&2
  else
    echo "[update] Refusing: could not check the app image '${app_image:-?}' for /init (docker exit ${init_rc})." >&2
  fi
  echo "[update] Nothing was changed; the snapshot is ${snapshot}." >&2
  exit 1
fi

# Start app ALONE first: only app runs migrations (plugin-host sets
# GDX_SKIP_MIGRATIONS), so exactly one container migrates. The app container
# starts its Celery workers and beat itself, after the migrations.
# secrets-init runs first through depends_on.
# --remove-orphans here, not only after health: an install still on the split
# layout has celery-* containers, and the old celery-beat beside the new app's
# beat would fire every scheduled task twice until they were removed.
echo "[update] Starting app (runs migrations)…"
"${COMPOSE[@]}" up -d --remove-orphans db redis app

# Budget generously: app applies migrations BEFORE it serves /health, and a
# real DDL migration can take minutes. Default ~10 min; override with HEALTH_TIMEOUT.
tries="$(( ${HEALTH_TIMEOUT:-600} / 5 ))"
echo "[update] Waiting up to ${HEALTH_TIMEOUT:-600}s for health (migrations run first)…"
healthy=0
# The container's own healthcheck (curl /health inside it), so this works
# whether or not the layout publishes a host port.
app_id="$("${COMPOSE[@]}" ps -q app)"
for _ in $(seq 1 "${tries}"); do
  if [ "$(docker inspect -f '{{.State.Health.Status}}' "${app_id}" 2>/dev/null)" = "healthy" ]; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "${healthy}" = "1" ]; then
  echo "[update] App healthy — starting the rest…"
  "${COMPOSE[@]}" up -d --remove-orphans
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
  echo "[update] plugin-host and backup were NOT started; rerun ./update.sh once /health is up." >&2
else
  echo "[update] App is not running — safe to roll back:" >&2
  echo "  1) pin the previous release in .env  (APP_VERSION=<old>) and check out its tag" >&2
  echo "     (git checkout v<old>): the compose files and the image must match, and an" >&2
  echo "     image from before the one-container layout cannot run these compose files" >&2
  echo "  2) gunzip -c ${snapshot} | ${COMPOSE[*]} exec -T db psql -U gdx gdx" >&2
  echo "  3) ${COMPOSE[*]} up -d" >&2
fi
exit 1
