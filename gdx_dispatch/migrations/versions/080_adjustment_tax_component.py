"""invoice_adjustments learns its tax share (money-audit M18).

Doug's ruling (2026-08-24): pro-rata at the invoice's rate — a credit
reduces tax by amount × (tax/total), and the migration backfills existing
rows THE SAME WAY (same arithmetic as core/invoice_tax.credit_tax_component,
so history and new writes cannot disagree). Prod exposure at decision time:
4 credited invoices, one carrying tax.

Revision ID: 080_adjustment_tax_component
Revises: 079_payment_plans
"""
import contextlib

from alembic import op

revision = "080_adjustment_tax_component"
down_revision = "079_payment_plans"
branch_labels = None
depends_on = None

_BACKFILL_PG = """
UPDATE invoice_adjustments a
SET tax_component = LEAST(
        ROUND(a.amount * i.tax_amount / i.total, 2),
        ROUND(i.tax_amount, 2)
    )
FROM invoices i
WHERE i.id = a.invoice_id
  AND a.tax_component = 0
  AND a.amount > 0
  AND i.total > 0
  AND i.tax_amount > 0;
"""

_BACKFILL_SQLITE = """
UPDATE invoice_adjustments
SET tax_component = (
    SELECT MIN(
        ROUND(invoice_adjustments.amount * 1.0 * i.tax_amount / i.total, 2),
        ROUND(i.tax_amount, 2)
    )
    FROM invoices i WHERE i.id = invoice_adjustments.invoice_id
)
WHERE tax_component = 0
  AND amount > 0
  AND EXISTS (
    SELECT 1 FROM invoices i
    WHERE i.id = invoice_adjustments.invoice_id
      AND i.total > 0 AND i.tax_amount > 0
  );
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE invoice_adjustments ADD COLUMN tax_component numeric(12,2) NOT NULL DEFAULT 0.00;"
            )
        bind.exec_driver_sql(_BACKFILL_SQLITE)
        return
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
            IF to_regclass('invoice_adjustments') IS NOT NULL THEN
                ALTER TABLE invoice_adjustments
                    ADD COLUMN IF NOT EXISTS tax_component numeric(12,2) NOT NULL DEFAULT 0.00;
            END IF;
        END $$;
        """
    )
    bind.exec_driver_sql(_BACKFILL_PG)


def downgrade() -> None:
    # Additive and zero-safe for older code — keep on downgrade.
    pass
