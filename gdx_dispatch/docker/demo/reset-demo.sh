#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Nightly demo reset — regenerate a FRESH DAY of demo data.
#
# Wipes the demo DB and RE-SEEDS from scratch, so every day the schedule, the
# tech's "Today's Route", and dashboard counts are relative to the CURRENT day
# (a frozen snapshot would freeze jobs on one date and go empty the next day).
#
# Hardcoded to the `gdx-demo` project/containers — it can never touch prod.
#
# Cron (nightly 08:00 UTC ≈ 03:00 ET):
#   0 8 * * * /var/www/gdx_dispatch/gdx_dispatch/docker/demo/reset-demo.sh >> /var/log/gdx-demo-reset.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT=gdx-demo
APP=gdx-demo-app-1
DB=gdx-demo-db-1
REDIS=gdx-demo-redis-1
COMPOSE="docker compose -p $PROJECT --env-file $DEMO_DIR/.env.demo -f $DEMO_DIR/docker-compose.demo.yml"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

log "Stopping app + plugin-host (release DB connections)…"
$COMPOSE stop app plugin-host >/dev/null

log "Flushing redis (stale tokens/cache)…"
docker exec "$REDIS" redis-cli FLUSHALL >/dev/null 2>&1 || true

log "Dropping & recreating an EMPTY demo database…"
docker exec "$DB" psql -U gdx -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS gdx WITH (FORCE);" \
  -c "CREATE DATABASE gdx OWNER gdx;"

log "Starting app (boot runs migrations + bootstrap on the empty DB)…"
$COMPOSE up -d app >/dev/null

# Fresh-DB migrations take longer than a snapshot restore — allow up to 3 min.
ok=0
for i in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8003/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [[ "$ok" != 1 ]]; then
  log "ERROR: app not healthy after boot — aborting BEFORE seed (demo left empty)."
  exit 1
fi

log "Seeding fresh, day-relative demo data…"
docker cp "$DEMO_DIR/seed_demo.py" "$APP:/tmp/seed_demo.py" >/dev/null
docker exec -e PYTHONPATH=/app -w /app "$APP" python /tmp/seed_demo.py

log "Bringing plugin-host back up…"
$COMPOSE up -d plugin-host >/dev/null

log "Demo reset complete — fresh day seeded."
