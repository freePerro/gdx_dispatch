"""record HOW an invoice was delivered

Billing Unsent tab + paper mail (2026-08-05): sent_at says WHEN an invoice
reached the customer but not HOW, so a paper invoice dropped in the mailbox
had no way to leave the "never delivered" bucket without lying about an
email. sent_via stores the channel: 'email' (server/Outlook send), 'mail'
(postal), 'manual' (operator says it went out some other way). Nullable:
NULL = delivered before this column existed (or never delivered — sent_at
NULL stays the delivery fact; sent_via only annotates it).

Revision ID: 057_invoice_sent_via
Revises: 056_money_correctness_rails
"""
from alembic import op

revision = "057_invoice_sent_via"
down_revision = "056_money_correctness_rails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invoices is in the squashed baseline, so it always exists here; IF NOT
    # EXISTS keeps the ALTER idempotent across multi-container boots.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sent_via varchar(20) NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS sent_via")
