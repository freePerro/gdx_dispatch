"""A cancelled job records when and why (GDXA-374, parent GDXA-373).

* ``jobs.cancelled_at`` — timestamptz, nullable. Stamped when a job moves to
  ``lifecycle_stage = 'cancelled'``.
* ``jobs.cancel_reason`` — varchar(300), nullable, the same width as
  ``not_billable_reason`` (055). The reason the office gave for cancelling.

Until now a cancel was a bare stage flip: nothing said when it happened or
why. The cancel endpoint that writes these columns is built on top of this
migration (jobs-dispatch); this migration only makes room for them.

Existing rows: both columns are NULL on every job, including jobs already in
the ``cancelled`` stage. Their cancel time and reason were never recorded, so
there is nothing true to backfill; ``updated_at`` is not a cancel time and is
not copied.

Portability: ``batch_alter_table``. The upgrade is a plain ``ALTER TABLE ...
ADD COLUMN`` on both engines (nullable, no default: Postgres does not rewrite
the table); only the downgrade's ``drop_column`` makes SQLite rebuild ``jobs``.
Guarded like 110: a no-op when ``jobs`` is absent or
already has the column; ``create_all`` runs before alembic on boot, so a fresh
database already has both.

Rollback: ``downgrade()`` drops both columns, discarding any cancel times and
reasons written since the upgrade. The stage stays ``cancelled``.

Revision ID: 111_job_cancel_lifecycle
Revises: 110_time_entry_billed_invoice
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "111_job_cancel_lifecycle"
down_revision = "110_time_entry_billed_invoice"
branch_labels = None
depends_on = None

_TABLE = "jobs"
_COLUMNS = (
    ("cancelled_at", sa.DateTime(timezone=True)),
    ("cancel_reason", sa.String(300)),
)


def _columns(bind) -> set[str] | None:
    """The jobs table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    return {col["name"] for col in insp.get_columns(_TABLE)}


def upgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None:
        return
    missing = [(name, type_) for name, type_ in _COLUMNS if name not in cols]
    if not missing:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name, type_ in missing:
            batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None:
        return
    present = [name for name, _ in reversed(_COLUMNS) if name in cols]
    if not present:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name in present:
            batch.drop_column(name)
