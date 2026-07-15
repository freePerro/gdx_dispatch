"""GL Phase 1 S8 — job_receipts.promoted_expense_id (promote-from-field)

job_receipts is a baseline table on existing DBs — real guarded ALTER.
expense_receipts itself is a NEW ORM table (create_orm_tables, #41).

Revision ID: 023_job_receipt_promotion
Revises: 022_payment_voided_at
"""
from alembic import op

revision = "023_job_receipt_promotion"
down_revision = "022_payment_voided_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $mig$
        BEGIN
          IF to_regclass('public.job_receipts') IS NULL THEN
            RAISE EXCEPTION
              'job_receipts table missing: create_orm_tables() must run before migration 023 (#41 ordering)';
          END IF;
          ALTER TABLE job_receipts ADD COLUMN IF NOT EXISTS promoted_expense_id UUID;
        END
        $mig$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE job_receipts DROP COLUMN IF EXISTS promoted_expense_id;"
    )
