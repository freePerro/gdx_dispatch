"""One boolean for the customer pay page: refuse US-issued debit cards.

* ``tenant_settings.refuse_debit_cards`` — BOOLEAN NOT NULL DEFAULT false.
  When true, the pay page's card tab refuses a card whose funding is
  ``debit`` and whose issuing country is ``US`` before any PaymentIntent is
  created, and tells the customer to use a credit card or bank transfer.
  Non-US debit is still taken: Visa and Mastercard's US limited-acceptance
  option lets a merchant take credit only, but a merchant taking any of
  their cards must take every valid card issued outside the US.

Existing rows get false, so nothing changes for any customer until the office
ticks the box. No money rows are touched.

Plain ``ADD COLUMN`` with a server default, on both engines. Guarded with
``has_table``/column checks because ``tenant_settings`` is an ORM-created
table: where ``create_all`` has not run yet it is absent, and ``create_all``
then builds it from the model with the column present.

Rollback: ``downgrade()`` drops the column (``batch_alter_table`` so SQLite
rebuilds the table). The only thing lost is the office's yes/no choice.

Revision ID: 104_refuse_debit_cards
Revises: 103_scheduled_sms
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "104_refuse_debit_cards"
down_revision = "103_scheduled_sms"
branch_labels = None
depends_on = None

TABLE = "tenant_settings"
COLUMN = "refuse_debit_cards"


def _has_column(bind) -> bool | None:
    """True/False, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(TABLE):
        return None
    return any(col["name"] == COLUMN for col in insp.get_columns(TABLE))


def upgrade() -> None:
    present = _has_column(op.get_bind())
    if present is None or present:
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(
            sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    if not _has_column(op.get_bind()):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
