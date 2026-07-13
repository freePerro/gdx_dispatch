"""Estimate "total-only" display — hide per-line prices on customer docs

Doug 2026-07-12. Operators want the option to send an estimate that shows the
line items (description + qty) plus a single bottom-line Total, WITHOUT the
per-line Unit Price / Line Total columns.

Three columns, one concept:

1. ``estimates.hide_line_prices`` — per-estimate override, NULLABLE tri-state:
   NULL = inherit the tenant default; TRUE = force hide; FALSE = force show.
   Mirrors the existing ``estimates.tax_rate`` / ``discount`` override pattern
   (NULL = use tenant default). ``estimates`` is a create_all (tenant-plane)
   table not in baseline_squashed.sql — guard with to_regclass (003/013 pattern)
   so a fresh-DB boot under GDX_SKIP_BOOTSTRAP is a no-op.

2. ``tenant_settings.estimates_hide_line_prices`` — the tenant-wide default,
   NOT NULL default false. ``tenant_settings`` IS in the baseline, so the plain
   ALTER (002 pattern) is safe.

3. ``invoices.hide_line_prices`` — NOT NULL default false. When an estimate with
   prices hidden converts to an invoice, the invoice-create handler snapshots the
   estimate's EFFECTIVE hide value onto this column so the invoice PDF matches the
   estimate. Independently editable afterward. ``invoices`` is create_all
   (tenant-plane) — guard with to_regclass.

Revision ID: 019_estimate_hide_line_prices
Revises: 018_drop_sticky_notes
"""
from alembic import op

revision = "019_estimate_hide_line_prices"
down_revision = "018_drop_sticky_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.estimates') IS NOT NULL THEN
            ALTER TABLE estimates
              ADD COLUMN IF NOT EXISTS hide_line_prices BOOLEAN;
          END IF;
          IF to_regclass('public.invoices') IS NOT NULL THEN
            ALTER TABLE invoices
              ADD COLUMN IF NOT EXISTS hide_line_prices BOOLEAN NOT NULL DEFAULT false;
          END IF;
        END $$;
        """
    )
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "ADD COLUMN IF NOT EXISTS estimates_hide_line_prices "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings DROP COLUMN IF EXISTS estimates_hide_line_prices"
    )
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.invoices') IS NOT NULL THEN
            ALTER TABLE invoices DROP COLUMN IF EXISTS hide_line_prices;
          END IF;
          IF to_regclass('public.estimates') IS NOT NULL THEN
            ALTER TABLE estimates DROP COLUMN IF EXISTS hide_line_prices;
          END IF;
        END $$;
        """
    )
