"""Drop seven dead ``users`` columns left from features never built (GDXA-1994).

``mfa_secret``, ``mfa_enabled``, ``auth_email``, ``google_email``,
``tc_permissions``, ``mcp_enabled`` and ``availability_status`` came over with
the production schema and were mapped on ``User`` without a reader or a
writer (triage GDXA-1864, maintainer ruling 2026-10-19): a repo-wide search
finds each name only at its model line and in the Postgres test fixture. Kept
and not to be confused with them: ``tc_can_view_others``/``tc_can_edit``/
``tc_can_approve`` (written by ``routers/admin_settings.py``) and
``technicians.availability_status`` (read by ``routers/technicians.py``). The
model drops the seven in the same change.

Same contract as 088, 091, 092 and 134: every present column is counted for
non-NULL values BEFORE anything is dropped, and any value refuses the whole
migration, dropping nothing. A value means someone stored something there by
hand, so a person must look. ``alembic upgrade head`` runs from the container
entrypoint under ``set -e`` and ``app`` restarts ``unless-stopped``, so a
refusal crash-loops the app until the values are exported and set to NULL;
that is the intended trade-off. Recount on prod and demo before deploying.

Portability: ``users`` is not in the squashed baseline, and a database built
by ``create_all`` after this change has none of the columns, so each one is
handled only when it exists. The drop is a native ``ALTER TABLE ... DROP
COLUMN`` on both engines (SQLite 3.35+), not ``batch_alter_table``: a SQLite
batch rebuilds the table, and ``users`` is the most-referenced table in the
schema. None of the seven carries an index, constraint or foreign key, which
is what SQLite's native drop requires.

Rollback: ``downgrade()`` re-adds the missing columns, nullable and empty, in
the types the model declared. Nothing is restored, because nothing was held.
Order matters once 136 has run. An image from before this change still maps
the seven on ``User``, so every user query fails against the dropped schema,
and its entrypoint's ``alembic upgrade head`` fails on any revision it does
not ship. A downgrade past other revisions breaks THIS image in turn (131's
downgrade drops a column this image's ``AppSettings`` maps), so neither image
can serve mid-rollback, and any restart of this image re-runs ``upgrade head``.
The order for prod, which names constraints rather than commands and has
not been rehearsed:

1. Stop every service of this release that connects (``app``, which runs
   the celery workers and beat under s6, ``plugin-host``, ``backup``).
2. Find the head revision of the image being pinned (``alembic heads`` in
   that image, run without its entrypoint, which touches the database). It
   is not necessarily 135: a release can predate several migrations.
3. From THIS image, in a one-off container on the same compose project,
   env file and compose files as ``docker/update.sh`` (``COMPOSE=``), run
   ``alembic downgrade`` to that revision. Every downgrade it passes runs;
   read them first. Start it with ``run --no-deps --entrypoint sh app -c
   'set -a; . "$GDX_RUNTIME_ENV_FILE"; set +a; cd /app/gdx_dispatch &&
   exec python -m alembic -c alembic.ini downgrade <rev>'`` (as entrypoint.sh
   runs it):
   the image's own entrypoint would boot the app (and its ``upgrade head``)
   first, and a bare ``--entrypoint alembic`` skips the runtime env that holds
   ``DATABASE_URL``, so alembic falls back to alembic.ini's dev URL.
4. Pin that ``APP_VERSION`` and start.

Demo is a separate stack on the same host: project ``gdx-demo``, env file
``docker/demo/.env.demo``, compose file ``docker/demo/docker-compose.demo.yml``,
no celery services. Step 3 with ``update.sh``'s project would downgrade
prod's database instead, so substitute demo's for every step there.
Demo sets no ``GDX_RUNTIME_ENV_FILE`` (its keys are compose env), so drop
the ``set -a ... set +a;`` part of step 3 there.

Revision ID: 136_drop_dead_user_columns
Revises: 135_job_return_visit_source
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "136_drop_dead_user_columns"
down_revision = "135_job_return_visit_source"
branch_labels = None
depends_on = None

_TABLE = "users"
# (column, type as the model declared it)
_COLUMNS = (
    ("auth_email", sa.String(254)),
    ("google_email", sa.String(254)),
    ("availability_status", sa.String(30)),
    ("mfa_enabled", sa.Boolean()),
    ("mfa_secret", sa.String(200)),
    ("tc_permissions", sa.Text()),
    ("mcp_enabled", sa.Boolean()),
)


def _columns(bind) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    return {col["name"] for col in insp.get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if cols is None:
        return
    present = [name for name, _ in _COLUMNS if name in cols]

    occupied: dict[str, int] = {}
    for name in present:
        held = bind.exec_driver_sql(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE {name} IS NOT NULL"  # noqa: S608 — names from a module constant
        ).scalar()
        if held:
            occupied[name] = int(held)
    if occupied:
        listing = ", ".join(f"{_TABLE}.{name} is set on {n} row(s)" for name, n in occupied.items())
        raise RuntimeError(
            f"136: {listing} — refusing to drop them. Nothing was dropped. "
            "Nothing in the app reads or writes these columns. Export each named "
            "column's non-NULL values with their user id, set the column to NULL "
            "on every row, then restart the container so the migration reruns."
        )

    for name in present:
        bind.exec_driver_sql(f"ALTER TABLE {_TABLE} DROP COLUMN {name}")


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if cols is None:
        return
    for name, type_ in _COLUMNS:
        if name not in cols:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))
