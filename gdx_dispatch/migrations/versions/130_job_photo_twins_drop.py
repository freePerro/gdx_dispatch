"""Drop ``job_photos.content_type`` and ``job_photos.file_size``, the Flask-era twins.

``job_photos`` carries two pairs of columns for one fact each: ``mime_type`` /
``content_type`` and ``size_bytes`` / ``file_size``. The only writer,
``core/job_photos.py::link_job_photo``, sets ``mime_type`` and
``size_bytes``; the twins were never written by this app (GDXA-1581). Their
one reader, the mobile job-detail photo query, now reads the real columns
under the old names, so its response shape does not change.

Existing rows: first every NULL ``mime_type`` takes its row's
``content_type`` and every NULL ``size_bytes`` its row's ``file_size``; a row
whose real column is already set keeps it. Then the drop is guarded like 088,
091 and 092: if any row still holds a twin value beside a NULL real value, the
backfill did not do its job, so the migration raises and nothing is dropped.
Be clear about what that means: ``alembic upgrade head`` runs from the
container entrypoint on every start, so a refusal means THE APP WILL NOT START
until someone reads ``docker logs`` and pins the previous ``APP_VERSION``. A
twin value that disagrees with a set real value is not copied and is lost with
the column: the real column is the one the app wrote and reads.

Portability: ``job_photos`` is not in the squashed baseline — production has
the Flask-era table and a fresh database gets it from ``create_all``, which
after this change builds it without the twins — so each pair is handled only
when the table and both of its columns exist. The ``UPDATE`` statements use no
functions or casts and are the same on both engines; ``batch_alter_table``
drops the columns in place on Postgres and rebuilds the table on SQLite
(nothing holds a foreign key to ``job_photos``).

Rollback: ``downgrade()`` re-adds both columns, nullable, and copies
``mime_type`` and ``size_bytes`` back into them. A twin value that disagreed
with its real column before the upgrade does not come back.

Revision ID: 130_job_photo_twins_drop
Revises: 129_job_photo_created_at_fill
Create Date: 2026-10-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "130_job_photo_twins_drop"
down_revision = "129_job_photo_created_at_fill"
branch_labels = None
depends_on = None

_TABLE = "job_photos"
# (real column, twin column, twin type as the Flask-era table declared it)
_PAIRS = (
    ("mime_type", "content_type", sa.String(150)),
    ("size_bytes", "file_size", sa.Integer()),
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
    pairs = [(real, twin) for real, twin, _ in _PAIRS if {real, twin} <= cols]
    if not pairs:
        return

    for real, twin in pairs:
        bind.exec_driver_sql(
            f"UPDATE {_TABLE} SET {real} = {twin} "  # noqa: S608 — names from a module constant
            f"WHERE {real} IS NULL AND {twin} IS NOT NULL"
        )

    for real, twin in pairs:
        stranded = bind.exec_driver_sql(
            f"SELECT COUNT(*) FROM {_TABLE} "  # noqa: S608 — names from a module constant
            f"WHERE {twin} IS NOT NULL AND {real} IS NULL"
        ).scalar()
        if stranded:
            raise RuntimeError(
                f"130: {stranded} {_TABLE} row(s) still hold {twin} with a NULL {real} "
                f"after the backfill — refusing to drop {twin}. Nothing was dropped. "
                f"Copy {twin} into {real} by hand on the rows where {real} is NULL, then "
                "restart the container so the migration reruns."
            )

    with op.batch_alter_table(_TABLE) as batch:
        for _, twin in pairs:
            batch.drop_column(twin)


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if cols is None:
        return
    for real, twin, type_ in _PAIRS:
        if twin not in cols:
            op.add_column(_TABLE, sa.Column(twin, type_, nullable=True))
        if real in cols:
            bind.exec_driver_sql(
                f"UPDATE {_TABLE} SET {twin} = {real}"  # noqa: S608 — names from a module constant
            )
