"""GL Phase 1 S6 — payments.voided_at (P4 payment void)

The payments table is a baseline table (exists on every DB), so this is a
real ALTER, guarded and idempotent. A voided payment stays as history but
stops counting toward the invoice balance and gets its P3 entry reversed.

Revision ID: 022_payment_voided_at
Revises: 021_gl_revenue_map
"""
from alembic import op

revision = "022_payment_voided_at"
down_revision = "021_gl_revenue_map"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $mig$
        BEGIN
          IF to_regclass('public.payments') IS NULL THEN
            RAISE EXCEPTION
              'payments table missing: create_orm_tables() must run before migration 022 (#41 ordering)';
          END IF;
          ALTER TABLE payments ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ;
        END
        $mig$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE payments DROP COLUMN IF EXISTS voided_at;"
    )
