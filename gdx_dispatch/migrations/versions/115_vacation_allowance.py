"""A yearly vacation allowance in hours: shop default and per-person override (GDXA-700).

* ``app_settings.vacation_allowance_hours`` — ``Numeric(6, 2) NULL``. The
  shop's annual vacation grant per calendar year, in hours. NULL means the
  shop has not set one, and readers say "no allowance set" rather than
  inventing a number.
* ``users.vacation_allowance_hours`` — ``Numeric(6, 2) NULL``. One person's
  override of that grant; NULL inherits the shop default, the same shape as
  the ``users.shift_start`` / ``shift_end`` / ``workdays`` overrides.

No accrual, carryover or payout rule is stored (GDXA-576: v1 is a flat
annual grant). Used and remaining hours are computed from closed ``vacation``
timeclock entries by the reader (GDXA-576 part 2), never stored here.

Existing rows: every ``app_settings`` and ``users`` row reads NULL — no shop
default and no override. No timeclock row, time-off request or money column is
read or written.

Portability: plain ``op.add_column`` of a nullable column with no default,
which both engines take without a table rebuild. Guarded per table: a no-op
when the table is absent or already has the column (``create_all`` builds it
from the model on a fresh database but never adds a column to an existing
table).

Rollback: ``downgrade()`` drops both columns (``batch_alter_table`` so SQLite
rebuilds the table). Any allowance an admin set is lost; the audit rows that
recorded each change stay. Code that reads the columns must be rolled back
first.

Revision ID: 115_vacation_allowance
Revises: 114_row_version_tokens
Create Date: 2026-10-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "115_vacation_allowance"
down_revision = "114_row_version_tokens"
branch_labels = None
depends_on = None

_TABLES = ("app_settings", "users")
_COLUMN = "vacation_allowance_hours"


def _columns(bind, table: str) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        cols = _columns(bind, table)
        if cols is None or _COLUMN in cols:
            continue
        op.add_column(table, sa.Column(_COLUMN, sa.Numeric(6, 2), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        cols = _columns(bind, table)
        if cols is None or _COLUMN not in cols:
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(_COLUMN)
