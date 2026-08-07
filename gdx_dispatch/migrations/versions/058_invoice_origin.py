"""where an invoice came from

Closeout autodraft (2026-08-07): closing out a job now auto-creates a draft
invoice priced from the closeout (labor lanes + priced closeout parts), so
Ready-for-Billing reviews an existing draft instead of starting from a blank
form. Re-closeouts rebuild the draft in place and Not-billable voids it —
but ONLY when the draft is machine-made and untouched (never verified, sent,
locked, or paid). origin records that provenance: 'closeout_autodraft' for
machine-drafted invoices; NULL for everything human-created (office, mobile,
QB import, deposits). Without the marker, a rebuild could destroy a draft an
operator typed by hand.

Revision ID: 058_invoice_origin
Revises: 057_invoice_sent_via
"""
from alembic import op

revision = "058_invoice_origin"
down_revision = "057_invoice_sent_via"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invoices is in the squashed baseline, so it always exists here; IF NOT
    # EXISTS keeps the ALTER idempotent across multi-container boots.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS origin varchar(32) NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS origin")
