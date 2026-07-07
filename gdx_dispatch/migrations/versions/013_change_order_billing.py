"""Change orders reach the invoice — billing link + tax flag

PR3-billing-capture (2026-07-07). Approved change orders were captured,
signed, then orphaned: no code path turned a ChangeOrderLine into an
InvoiceLine, so mid-job scope increases had to be hand-keyed or were never
billed. This migration adds the storage for the S122-style billing link:

1. ``change_orders.billed_invoice_id`` — the invoice this CO was billed on.
   Same shape/semantics as ``job_parts_needed.billed_invoice_id``: set by the
   invoice-create handler (stamp GATES the line copy — UPDATE…RETURNING, so a
   CO can never bill twice); FK ON DELETE SET NULL so a hard-deleted invoice
   releases its COs (soft-delete handler does the same). Indexed for the
   ``unbilled=true`` filter.
2. ``change_order_lines.taxable`` — Doug 2026-07-07: COs are handled like
   invoices, tax shown at signature time. Default TRUE mirrors InvoiceLine.

Both tables are NOT in baseline_squashed.sql — they're created by
create_orm_tables() (create_all) at boot, which on a fresh DB builds them
WITH these columns before alembic runs (#41 ordering). Guard with
to_regclass so the fresh-DB boot is a no-op (003 pattern).

Revision ID: 013_change_order_billing
Revises: 012_gl_core
"""
from alembic import op

revision = "013_change_order_billing"
down_revision = "012_gl_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.change_orders') IS NOT NULL THEN
            ALTER TABLE change_orders
              ADD COLUMN IF NOT EXISTS billed_invoice_id UUID
                REFERENCES invoices(id) ON DELETE SET NULL;
            CREATE INDEX IF NOT EXISTS ix_change_orders_billed_invoice_id
              ON change_orders (billed_invoice_id);
          END IF;
          IF to_regclass('public.change_order_lines') IS NOT NULL THEN
            ALTER TABLE change_order_lines
              ADD COLUMN IF NOT EXISTS taxable BOOLEAN NOT NULL DEFAULT true;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.change_orders') IS NOT NULL THEN
            DROP INDEX IF EXISTS ix_change_orders_billed_invoice_id;
            ALTER TABLE change_orders DROP COLUMN IF EXISTS billed_invoice_id;
          END IF;
          IF to_regclass('public.change_order_lines') IS NOT NULL THEN
            ALTER TABLE change_order_lines DROP COLUMN IF EXISTS taxable;
          END IF;
        END $$;
        """
    )
