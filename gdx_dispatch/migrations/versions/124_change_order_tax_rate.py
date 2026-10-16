"""A change order stores the tax rate it quotes (GDXA-795).

* ``change_orders.tax_rate`` — ``Numeric(9, 6) NULL``, a decimal fraction
  (0.0738 is 7.38 percent), the same type as ``estimates.tax_rate`` and
  ``invoices.tax_rate``. ``routers/change_orders.py`` stamps it on create, on
  a re-link of an unsigned CO to another job or customer, and again when the
  CO is approved (so the signed number is current), through
  ``modules/tax/service.resolve_rate_for_job`` with the job's accepted
  estimate's rate as the quote: install lane 0, exempt customer 0, else the
  quoted rate, else the tenant default — what invoice create charges. Before
  this the CO detail resolved live on every read and never saw the estimate,
  so a CO on a repair quoted at zero showed and was signed with tax the
  invoice never charged.

Existing rows: every ``change_orders`` row reads NULL, and NULL means "written
before 124". Nothing is backfilled. A signed (approved or completed) NULL row
keeps the live resolve it always had, so no signed number moves; rewriting
it here would make the signed number and the shown one disagree. An unsigned
row, like every unsigned CO from here on, shows the live rate approval would
stamp, which now includes the job's accepted estimate's rate; it is stamped
when it is next re-linked or approved. No invoice, estimate or line row is
read or written.

Portability: plain ``op.add_column`` of a nullable column with no default,
which both engines take without a table rebuild. A no-op when the table is
absent or already has the column (``create_all`` builds it from the model on
a fresh database but never adds a column to an existing table).

Rollback: ``downgrade()`` drops the column (``batch_alter_table`` so SQLite
rebuilds the table). Every stamped rate is lost and those COs go back to the
live resolve, which follows the lane but not the estimate, i.e. the defect. The audit rows that recorded
each stamp stay. Code that reads the column must be rolled back first.

Revision ID: 124_change_order_tax_rate
Revises: 123_audit_chain_serialized
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "124_change_order_tax_rate"
down_revision = "123_audit_chain_serialized"
branch_labels = None
depends_on = None

_TABLE = "change_orders"
_COLUMN = "tax_rate"


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
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Numeric(9, 6), nullable=True))


def downgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None or _COLUMN not in cols:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
