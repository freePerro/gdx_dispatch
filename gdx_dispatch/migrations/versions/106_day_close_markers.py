"""Day-close markers for multi-day jobs (docs/design/multi-day-jobs-plan.md §5.4a).

* ``time_entries.day_closed_at`` — timestamp, nullable. Set by day-close
  ("No" on the closeout sheet) on every timer it consumes and every hours row
  it writes, to the submission's raw ``closed_at``. A row with it set and
  ``duration_minutes`` > 0 is a *day row*: one person's attested hours for one
  shop day. A timer consumed at 0 carries it too, so it is never picked,
  restated or listed again.
* ``appointments.day_closed_at`` — timestamp, nullable. The raw ``closed_at``
  of the day-close that closed the visit. It makes a visits-only submission
  replayable and lets a second closer be told who closed it first.

Nothing but day-close writes either column. No backfill: before this
migration nothing was day-closed, so every existing row keeps NULL and every
existing job bills exactly as it did.

Guarded with ``has_table``/column checks: both tables are ORM-created, and
``create_all`` runs before alembic on boot (``entrypoint.sh``), so on a fresh
install the columns already exist and this is a no-op.

Rollback: ``downgrade()`` drops both columns (``batch_alter_table`` so SQLite
rebuilds the table). Day rows then read as plain closed rows; their minutes
stay, and the ``user_id`` NULL ones stay unpaid.

Revision ID: 106_day_close_markers
Revises: 105_location_local_edit
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "106_day_close_markers"
down_revision = "105_location_local_edit"
branch_labels = None
depends_on = None

TABLES = ("time_entries", "appointments")
COLUMN = "day_closed_at"


def _missing(bind, table: str) -> bool | None:
    """True when the table exists without the column, None when it is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return COLUMN not in {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        if _missing(bind, table):
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        if _missing(bind, table) is False:
            with op.batch_alter_table(table) as batch:
                batch.drop_column(COLUMN)
