"""Vendor invoice intake — Expense.source + vendors.name_aliases

vendor-invoice-intake (2026-07-08). Supplier PDF bills (e.g. Midwest Wholesale
Doors retail-sale invoices) get parsed and their lines routed to a job (cost +
billing), to stock (inventory receipt), or to overhead.

The two new TABLES — ``vendor_invoices`` and ``vendor_invoice_lines`` — are NOT
in this migration: they are ORM-created (TenantBase.metadata, #41 ordering,
same as the sibling ``vendor_statements`` tables which also have no migration).
create_orm_tables() builds them before alembic runs.

This migration only adds two COLUMNS to existing baseline/ORM tables, which
create_all cannot add on an already-existing prod table:

1. ``expenses.source`` — 'manual' | 'vendor_invoice'. The QuickBooks-push
   boundary: a vendor-invoice expense is already mirrored to QB by the banking
   sync, so a future expense-push must anti-join this value. NOT NULL DEFAULT
   'manual' (every existing row was keyed by hand).
2. ``vendors.name_aliases`` — JSON array (Text) of alternate spellings a vendor
   bills under, so a parsed/LLM-read name resolves back to the vendor row.

Both guarded with to_regclass so a fresh ORM-built DB no-ops (003/014 pattern).

Revision ID: 024_vendor_invoice_intake
Revises: 023_job_receipt_promotion
"""
from alembic import op

revision = "024_vendor_invoice_intake"
down_revision = "023_job_receipt_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.expenses') IS NOT NULL THEN
            ALTER TABLE expenses
              ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'manual';
          END IF;
          IF to_regclass('public.vendors') IS NOT NULL THEN
            ALTER TABLE vendors
              ADD COLUMN IF NOT EXISTS name_aliases TEXT;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.expenses') IS NOT NULL THEN
            ALTER TABLE expenses DROP COLUMN IF EXISTS source;
          END IF;
          IF to_regclass('public.vendors') IS NOT NULL THEN
            ALTER TABLE vendors DROP COLUMN IF EXISTS name_aliases;
          END IF;
        END $$;
        """
    )
