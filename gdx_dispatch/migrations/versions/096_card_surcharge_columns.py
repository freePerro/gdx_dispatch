"""Two nullable columns for the credit-card surcharge on the customer pay page.

* ``payments.surcharge_amount`` — the card processing fee the customer paid on
  top of ``amount``. ``amount`` stays what settles the invoice; the fee posts
  to 4950 Card Surcharge Income. NULL on every existing row (no surcharge was
  ever collected before this).
* ``tenant_settings.card_surcharge_percent`` — the rate, as a fraction
  (0.029 = 2.9 percent). NULL/0 = no surcharge. Ships NULL so nothing changes for any
  customer until the office turns it on after Visa's 30-day acquirer notice.

Plain ``ADD COLUMN`` on both engines. Guarded with ``has_table``/column checks
because ``payments`` and ``tenant_settings`` are ORM-created tables: on a
database where ``create_all`` has not run yet they are absent, and
``create_all`` then builds them from the model with the columns present.

Rollback: ``downgrade()`` drops both columns. On SQLite that goes through
``batch_alter_table`` (table rebuild); on Postgres it is a direct DROP.

Revision ID: 096_card_surcharge_columns
Revises: 095_drop_campaigns_mobile_sync
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "096_card_surcharge_columns"
down_revision = "095_drop_campaigns_mobile_sync"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("payments", "surcharge_amount", sa.Numeric(12, 2)),
    ("tenant_settings", "card_surcharge_percent", sa.Numeric(5, 4)),
)


def _has_column(bind, table: str, column: str) -> bool | None:
    """True/False, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    for table, column, type_ in _COLUMNS:
        present = _has_column(bind, table, column)
        if present is None or present:
            continue
        op.add_column(table, sa.Column(column, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table, column, _type in _COLUMNS:
        if not _has_column(bind, table, column):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
