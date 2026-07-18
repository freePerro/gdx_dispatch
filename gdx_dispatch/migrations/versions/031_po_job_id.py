"""add job_id to purchase_orders

A purchase order can be ordered *for a job* (the "Order Doors" case) rather than
just for vendor stock. Recording that job lets receiving follow the thread to the
job's captured door specs — what should arrive + how heavy — the same estimate→
Job→line_metadata chain the tech/office install views read. Nullable: vendor-stock
POs simply leave it NULL.

Revision ID: 031_po_job_id
Revises: 030_customer_contacts
"""
from alembic import op

revision = "031_po_job_id"
down_revision = "030_customer_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # purchase_orders is created by SQLAlchemy create_all() at app startup, NOT
    # in baseline_squashed.sql. On a fresh DB the entrypoint runs `alembic
    # upgrade head` BEFORE the app boots, so the table doesn't exist yet — guard
    # the ALTER so the migration is a no-op then (create_all builds the table
    # WITH job_id from the model). On existing DBs the table is present and the
    # ALTER runs. IF NOT EXISTS keeps it idempotent across multi-container boots.
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.purchase_orders') IS NOT NULL THEN
            ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS job_id uuid;
            CREATE INDEX IF NOT EXISTS ix_purchase_orders_job_id
              ON purchase_orders (job_id);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DROP INDEX IF EXISTS ix_purchase_orders_job_id"
    )
    op.get_bind().exec_driver_sql(
        "ALTER TABLE purchase_orders DROP COLUMN IF EXISTS job_id"
    )
