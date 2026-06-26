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
    # #41 — create the ORM-managed (tenant-plane) tables BEFORE alembic, so any
    # migration that ALTERs a non-baseline table finds it present on a fresh DB.
    # The squashed baseline only creates the disjoint control-plane tables, so
    # this does not collide with migration 001.
    if [ "${GDX_SKIP_BOOTSTRAP:-}" != "1" ]; then
        echo "[entrypoint] Pre-migration: ensuring ORM tables exist (create_all)…"
        python -c "from gdx_dispatch.tools.bootstrap_app import create_orm_tables; create_orm_tables()"
    fi

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
