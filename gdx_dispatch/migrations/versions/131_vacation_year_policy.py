"""Which year a vacation allowance counts over: a company setting (GDXA-1827).

* ``app_settings.vacation_year_policy`` — ``VARCHAR(20) NOT NULL DEFAULT
  'calendar_year'``. ``calendar_year`` (1 January) or ``hire_anniversary``
  (each person's ``users.hire_date``, falling back to the calendar year for
  anyone with no hire date). Doug ruled 2026-10-19 that each company chooses
  and calendar year is the default.

``users.hire_date`` already exists (``DATE NULL``); this revision does not
touch it. GDXA-1827 gives it a writer (``PATCH /api/users/{id}``).

Existing rows: the server default fills every ``app_settings`` row with
``calendar_year``, which is the year the allowance already counts over, so no
balance changes. No user, timeclock or money row is read or written.

Portability: ``op.add_column`` of a ``NOT NULL`` column with a constant
server default; SQLite and Postgres both take it without a table rebuild.
Guarded: a no-op when the table is absent or already has the column
(``create_all`` builds it from the model on a fresh database).

Rollback: ``downgrade()`` drops the column (``batch_alter_table`` so SQLite
rebuilds the table). A company that chose ``hire_anniversary`` loses that
choice; the audit rows that recorded it stay. Code that reads the column must
be rolled back first.

Revision ID: 131_vacation_year_policy
Revises: 130_job_photo_twins_drop
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "131_vacation_year_policy"
down_revision = "130_job_photo_twins_drop"
branch_labels = None
depends_on = None

_TABLE = "app_settings"
_COLUMN = "vacation_year_policy"


def _columns(bind) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    return {col["name"] for col in insp.get_columns(_TABLE)}


def upgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None or _COLUMN in cols:
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(20), nullable=False, server_default="calendar_year"),
    )


def downgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None or _COLUMN not in cols:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
