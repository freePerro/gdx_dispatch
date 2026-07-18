"""add door_snapshot to purchase_orders

Freezes the door receiving specs onto a PO at the moment it's linked to a job,
so receiving validates against what THIS PO ordered — not whatever the job's
latest estimate says at receive time. Revising an accepted quote after the PO is
cut must not silently move the target. Nullable JSON: only job-linked POs carry
it; vendor-stock POs leave it NULL. Mirrors migration 031's guarded pattern.

Revision ID: 032_po_door_snapshot
Revises: 031_po_job_id
"""
from alembic import op

revision = "032_po_door_snapshot"
down_revision = "031_po_job_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # purchase_orders is create_all-managed, not in baseline_squashed.sql. On a
    # fresh DB `alembic upgrade head` runs before the app boots (table absent) —
    # guard the ALTER so it's a no-op then (create_all builds the column from the
    # model). On existing DBs the table is present and the ALTER runs. IF NOT
    # EXISTS keeps it idempotent across multi-container boots.
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.purchase_orders') IS NOT NULL THEN
            ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS door_snapshot json;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE purchase_orders DROP COLUMN IF EXISTS door_snapshot"
    )
