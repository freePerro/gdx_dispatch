"""Service-call sell pricing + correction traceability — plan §8/§12.

* pricing_settings.service_call_first_hour_price / service_call_hourly_rate —
  the §8 service lane's SELL rates (Doug 2026-07-29: first-hour minimum,
  then hourly; both $100 today, two columns so they can diverge by settings
  change). They live beside target_labor_blended_rate_per_hour, the
  established persisted home for pricing config — NOT routers/pricing.py's
  in-memory dict.

* invoices.adjusts_invoice_id — readied for §12's correction flow (piece
  7c): a supplemental invoice (hours went UP after billing) will point at
  the invoice it corrects, and the credit-memo path will stamp it too. NOT
  yet written or read by any code — the column lands now so the flow is a
  pure code change later. A plain nullable column ON PURPOSE: adding an enum
  value to billing_type would be a Postgres ADD VALUE inside alembic's
  single transaction (the known hazard), and a correction IS a standard
  invoice with provenance.

IF NOT EXISTS on all three — same create_all-vs-alembic split as 040–043.

Revision ID: 044_service_pricing_and_adjustments
Revises: 043_closeout_verification_rails
"""
from alembic import op

revision = "044_service_pricing_and_adjustments"
down_revision = "043_closeout_verification_rails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE pricing_settings ADD COLUMN IF NOT EXISTS "
        "service_call_first_hour_price numeric(8,2) NOT NULL DEFAULT 100"
    )
    bind.exec_driver_sql(
        "ALTER TABLE pricing_settings ADD COLUMN IF NOT EXISTS "
        "service_call_hourly_rate numeric(8,2) NOT NULL DEFAULT 100"
    )
    bind.exec_driver_sql(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS adjusts_invoice_id uuid"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS adjusts_invoice_id")
    bind.exec_driver_sql(
        "ALTER TABLE pricing_settings DROP COLUMN IF EXISTS service_call_hourly_rate"
    )
    bind.exec_driver_sql(
        "ALTER TABLE pricing_settings DROP COLUMN IF EXISTS service_call_first_hour_price"
    )
