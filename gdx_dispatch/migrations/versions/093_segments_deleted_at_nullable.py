"""Make `segments.deleted_at` nullable so a segment can be created at all.

No migration ever created the `segments` table — `create_all` at boot did
(`tools/bootstrap_app.py`, `tools/pave_tenant_db.py`) — so the column shape
came straight from the ORM annotation. `Segment.deleted_at` was declared
`Mapped[datetime]` with no `| None` and no `nullable=True`, and SQLAlchemy 2.0
infers NOT NULL from a non-Optional annotation. Measured on the dev Postgres
2026-09-07: `deleted_at ... is_nullable=NO`.

Two consequences, both fatal to the feature:

  * `create_segment` never sets `deleted_at`, so every create raised
    IntegrityError. Probed: `NOT NULL constraint failed: segments.deleted_at`.
  * Every read filters `deleted_at IS NULL`, which a NOT NULL column can
    never satisfy — so no custom segment could ever be listed, and
    `delete_segment` had nothing to soft-delete.

The table holds **0 rows** on the dev database (measured 2026-09-07), which is
exactly what a feature that cannot insert looks like. Nothing to back-fill.

Portability: SQLite has no `ALTER COLUMN`, so this uses `batch_alter_table`,
which Alembic implements there by recreating the table and copying rows.
Postgres takes the `ALTER ... DROP NOT NULL` path directly. Guarded with
`has_table` for the same reason 091 was: no migration owns this table, so on a
database where `create_all` has not run yet it simply is not there, and
`create_all` will then build it correctly from the fixed model.

Rollback: `downgrade()` restores NOT NULL. It first stamps any NULL
`deleted_at` with the epoch, because restoring the constraint over a NULL
would fail — and a row stamped that way is one an older image would treat as
deleted, which is the honest outcome given an older image cannot represent
"live" in this column at all.

Revision ID: 093_segments_deleted_at_nullable
Revises: 092_drop_dead_duplicate_tables
Create Date: 2026-09-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "093_segments_deleted_at_nullable"
down_revision = "092_drop_dead_duplicate_tables"
branch_labels = None
depends_on = None

_TABLE = "segments"
_COLUMN = "deleted_at"


def _column_is_nullable(bind) -> bool | None:
    """True/False, or None when the table or column is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    for col in insp.get_columns(_TABLE):
        if col["name"] == _COLUMN:
            return bool(col["nullable"])
    return None


def upgrade() -> None:
    bind = op.get_bind()
    nullable = _column_is_nullable(bind)
    if nullable is None or nullable is True:
        # Table not created yet (create_all will build it from the fixed
        # model), or already relaxed. Either way there is nothing to do.
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            _COLUMN,
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    nullable = _column_is_nullable(bind)
    if nullable is None or nullable is False:
        return
    # Restoring NOT NULL over a NULL would fail; stamp live rows instead.
    bind.exec_driver_sql(
        f"UPDATE {_TABLE} SET {_COLUMN} = '1970-01-01 00:00:00' "  # noqa: S608 — names are module constants
        f"WHERE {_COLUMN} IS NULL"
    )
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            _COLUMN,
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
