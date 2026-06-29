#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Nightly demo reset — wipe whatever visitors did and restore the golden state.
#
# Restores a pre-seeded snapshot (golden.sql.gz) into the ISOLATED demo db.
# Hardcoded to the `gdx-demo` project/containers — it can never touch prod.
#
# Build the golden snapshot ONCE after seeding:
#   docker exec gdx-demo-db-1 pg_dump -U gdx -d gdx | gzip > golden.sql.gz
#
# Cron (nightly 08:00 UTC ≈ 03:00 ET):
#   0 8 * * * /var/www/gdx_dispatch/gdx_dispatch/docker/demo/reset-demo.sh >> /var/log/gdx-demo-reset.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT=gdx-demo
DB_CONTAINER=gdx-demo-db-1
GOLDEN="${GOLDEN:-$DEMO_DIR/golden.sql.gz}"
COMPOSE="docker compose -p $PROJECT --env-file $DEMO_DIR/.env.demo -f $DEMO_DIR/docker-compose.demo.yml"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# Guard: never wipe without a restore source.
if [[ ! -s "$GOLDEN" ]]; then
  log "ABORT: golden snapshot missing/empty at $GOLDEN — refusing to wipe."
  exit 1
fi

log "Stopping demo app (release DB connections)…"
$COMPOSE stop app >/dev/null

log "Dropping & recreating demo database…"
docker exec "$DB_CONTAINER" psql -U gdx -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS gdx WITH (FORCE);" \
  -c "CREATE DATABASE gdx OWNER gdx;"

log "Restoring golden snapshot ($(du -h "$GOLDEN" | cut -f1))…"
gunzip -c "$GOLDEN" | docker exec -i "$DB_CONTAINER" psql -U gdx -d gdx -q -v ON_ERROR_STOP=1 >/dev/null

log "Starting demo app…"
$COMPOSE start app >/dev/null

# Wait for health (app re-runs alembic [no-op at head] + idempotent bootstrap).
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8003/health >/dev/null 2>&1; then
    log "Demo reset complete — app healthy."
    exit 0
  fi
  sleep 2
done
log "WARNING: app did not report healthy within 60s after reset."
exit 1
