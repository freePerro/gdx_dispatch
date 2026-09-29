"""Outlook settings — payment-confirmation sender allowlist column

Vendor Statements now sets what the payment portal's confirmation emails say
was SENT beside what each statement says was APPLIED. Which senders count as a
payment portal is tenant data, not code (see
docs/design/vendor-payment-visibility-plan.md §B), so it is a list on
``outlook_settings``; empty = off.

``outlook_settings`` is ORM-created (not in the squashed baseline), so on a
fresh DB create_orm_tables() already builds the column and this migration is a
guarded no-op. Inspector-guarded rather than a ``DO $$`` block so it runs on
SQLite as well as Postgres. Existing rows get ``[]`` — the feature starts off.

Revision ID: 100_payment_confirm_senders
Revises: 099_lead_intake
"""
import sqlalchemy as sa
from alembic import op

revision = "100_payment_confirm_senders"
down_revision = "099_lead_intake"
branch_labels = None
depends_on = None

_TABLE = "outlook_settings"
_COLUMN = "payment_confirmation_sender_allowlist"


def _columns() -> set[str] | None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_TABLE):
        return None
    return {c["name"] for c in insp.get_columns(_TABLE)}


def upgrade() -> None:
    cols = _columns()
    if cols is None or _COLUMN in cols:
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    cols = _columns()
    if cols is None or _COLUMN not in cols:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
