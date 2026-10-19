#!/usr/bin/env bash
# Regenerate gdx_dispatch/tests/fixtures/structure.sql — the schema the PG-truth
# fixture (gdx_dispatch/tests/fixtures/pg.py) loads into its template database.
#
# The dump is `pg_dump --schema-only` of a database built by nothing but
# TenantBase.metadata.create_all(), so the ORM is the only input. Run it after
# any change to a TenantBase model; gdx_dispatch/tests/test_structure_sql_drift.py
# goes red until you do (GDXA-1558: the file lagged the ORM by 103 tables for a
# month because this script was named in pg.py's docstring and never existed).
#
# Everything runs in throwaway containers on a throwaway network: a fresh
# postgres:16-alpine (the major CI's service runs) and the docker-app image,
# which supplies the Python and pg_dump. No database you already have is read
# or written.
#
# Usage, from the repo root or a worktree root:
#   gdx_dispatch/tools/refresh_test_schema.sh
# Env: IMAGE (default docker-app), PG_IMAGE (default postgres:16-alpine).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
IMAGE="${IMAGE:-docker-app}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
OUT="$ROOT/gdx_dispatch/tests/fixtures/structure.sql"
TAG="gdx-refresh-schema-$$"
TMP=""

cleanup() {
    [ -n "$TMP" ] && rm -f "$TMP"
    docker rm -f "$TAG-pg" >/dev/null 2>&1 || true
    docker network rm "$TAG" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$TAG" >/dev/null
docker run -d --name "$TAG-pg" --network "$TAG" \
    -e POSTGRES_USER=gdx -e POSTGRES_PASSWORD=gdx -e POSTGRES_DB=gdx \
    "$PG_IMAGE" >/dev/null

TMP="$(mktemp "$OUT.XXXXXX")"
# The key fixes pg_dump's \restrict line (16.10+), which is otherwise random on
# every run and would make each regeneration a diff.
docker run --rm --network "$TAG" --entrypoint bash \
    -v "$ROOT":/app -w /app -e PYTHONPATH=/app \
    -e JWT_SECRET=refresh-test-schema-not-a-secret-xxxxxxxx \
    -e PGPASSWORD=gdx \
    "$IMAGE" -c '
set -euo pipefail
for _ in $(seq 60); do pg_isready -q -h "$0" -U gdx -d gdx && break; sleep 1; done
python - "$0" <<"PY"
import sys
from sqlalchemy import create_engine
import gdx_dispatch.models  # noqa: F401 — registers every model on TenantBase.metadata
from gdx_dispatch.core.audit import TenantBase
engine = create_engine(f"postgresql+psycopg2://gdx:gdx@{sys.argv[1]}:5432/gdx")
TenantBase.metadata.create_all(engine)
engine.dispose()
PY
pg_dump -h "$0" -U gdx -d gdx --schema-only --no-owner --no-privileges \
    --restrict-key=gdxteststructure
' "$TAG-pg" >"$TMP"

grep -q '^CREATE TABLE public\.' "$TMP" || { echo "pg_dump produced no tables" >&2; exit 1; }
chmod 644 "$TMP"
mv "$TMP" "$OUT"
TMP=""
echo "wrote $OUT ($(grep -c '^CREATE TABLE public\.' "$OUT") tables)"
