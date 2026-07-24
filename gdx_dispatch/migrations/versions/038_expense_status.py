"""expenses.status — the approval workflow finally has a column

Write-path data-loss fix from the 2026-07-24 contract-gap sweep (Tier 6):
ExpensesView has rendered a Draft→Submitted→Approved→Reimbursed approval
pipeline since it shipped, sending a status the backend silently dropped —
every approval reverted to Draft on reload because there was no column to
hold it.

(An earlier draft of this migration also added customers.access_notes; the
adversarial audit caught that access_notes lives on CustomerLocation by a
deliberate 2026-05-21 decision — the customer-level field was a missed UI
cleanup, not a missing column, so it was removed from the form instead.)

The table is created by SQLAlchemy create_all() at app startup — same
fresh-DB ordering guard as 036/037: no-op before the table exists
(create_all builds it WITH the column from the model). IF NOT EXISTS keeps
it idempotent across multi-container boots.

Revision ID: 038_expense_status
Revises: 037_notification_delete_sms_read
"""
from alembic import op

revision = "038_expense_status"
down_revision = "037_notification_delete_sms_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.expenses') IS NOT NULL THEN
            ALTER TABLE expenses
              ADD COLUMN IF NOT EXISTS status varchar(20) NOT NULL DEFAULT 'draft';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.expenses') IS NOT NULL THEN
            ALTER TABLE expenses DROP COLUMN IF EXISTS status;
          END IF;
        END $$;
        """
    )
