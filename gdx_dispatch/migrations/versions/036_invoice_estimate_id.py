"""add estimate_id to invoices

Deposit invoices (2026-07-23): a downpayment collected at estimate
acceptance becomes an Invoice with billing_type='deposit'. A mobile-accepted
quote may have no job yet, so job_id can't be the only thread back to the
sale — estimate_id records which estimate the invoice was born from. When
the estimate later converts to a job, _create_job_from_estimate adopts the
orphan deposit invoice (job_id backfilled via this column) so final-invoice
netting can find it. Nullable: NULL means "not estimate-derived" and every
pre-feature row.

Revision ID: 036_invoice_estimate_id
Revises: 035_job_created_by
"""
from alembic import op

revision = "036_invoice_estimate_id"
down_revision = "035_job_created_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invoices is created by SQLAlchemy create_all() at app startup, NOT in
    # baseline_squashed.sql — same fresh-DB ordering as 031_po_job_id: guard
    # so the migration no-ops before the table exists (create_all builds it
    # WITH estimate_id from the model). IF NOT EXISTS keeps it idempotent
    # across multi-container boots.
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.invoices') IS NOT NULL THEN
            ALTER TABLE invoices ADD COLUMN IF NOT EXISTS estimate_id uuid;
            CREATE INDEX IF NOT EXISTS ix_invoices_estimate_id
              ON invoices (estimate_id);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DROP INDEX IF EXISTS ix_invoices_estimate_id"
    )
    op.get_bind().exec_driver_sql(
        "ALTER TABLE invoices DROP COLUMN IF EXISTS estimate_id"
    )
