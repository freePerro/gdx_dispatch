#!/bin/sh
# First-run / every-boot container entrypoint.
#
# A bare `uvicorn` start against an empty Postgres can't serve login: the
# control-plane tables come from Alembic, the tenant-plane tables (users,
# companies, …) come from the ORM, and there's no tenant/admin to log in as.
# This script makes `docker compose up` produce a working app by running, in
# order: migrations → ORM table create → seed default tenant/company/admin →
# then exec the real command (CMD).
#
# Every step is idempotent, so this is safe to run on each container start.
set -e

# Alembic should migrate the SAME database the app uses. Pinning
# ALEMBIC_DATABASE_URL to DATABASE_URL also avoids relying on the dev
# fallback URL baked into alembic.ini.
if [ -n "${DATABASE_URL:-}" ]; then
    export ALEMBIC_DATABASE_URL="${ALEMBIC_DATABASE_URL:-$DATABASE_URL}"
fi

if [ "${GDX_SKIP_MIGRATIONS:-}" = "1" ]; then
    echo "[entrypoint] GDX_SKIP_MIGRATIONS=1 — skipping alembic upgrade."
else
    echo "[entrypoint] Running database migrations (alembic upgrade head)…"
    # script_location in alembic.ini is relative ('migrations'), so run from
    # the directory that contains it.
    ( cd /app/gdx_dispatch && python -m alembic -c alembic.ini upgrade head )
fi

if [ "${GDX_SKIP_BOOTSTRAP:-}" = "1" ]; then
    echo "[entrypoint] GDX_SKIP_BOOTSTRAP=1 — skipping schema bootstrap."
else
    echo "[entrypoint] Bootstrapping schema + default tenant/admin…"
    python -m gdx_dispatch.tools.bootstrap_app
fi

echo "[entrypoint] Starting application: $*"
exec "$@"
