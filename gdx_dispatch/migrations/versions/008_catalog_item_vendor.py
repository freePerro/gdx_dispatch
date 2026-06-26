"""#55 — first-class vendor field on catalog items

Adds custom_catalog_items.vendor (nullable). Same as 005/006/007:
custom_catalog_items is built by create_all, not the squashed baseline, so
guard the ALTER with to_regclass (no-op on a fresh DB where the table doesn't
exist yet; runs on existing DBs). Idempotent for the multi-container boot.

Revision ID: 008_catalog_item_vendor
Revises: 007_catalog_active
"""
from alembic import op

revision = "008_catalog_item_vendor"
down_revision = "007_catalog_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.custom_catalog_items') IS NOT NULL THEN
            ALTER TABLE custom_catalog_items
              ADD COLUMN IF NOT EXISTS vendor VARCHAR(200);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE custom_catalog_items DROP COLUMN IF EXISTS vendor"
    )
