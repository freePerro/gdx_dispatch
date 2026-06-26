"""#50 — whole-catalog active/inactive flag

Adds custom_catalogs.active (default true). Same as migrations 005/006:
custom_catalogs is built by create_all, not the squashed baseline, so guard the
ALTER with to_regclass (no-op on a fresh DB where the table doesn't exist yet;
runs on existing DBs). Idempotent for the multi-container boot.

Revision ID: 007_catalog_active
Revises: 006_catalog_pricing_strategy
"""
from alembic import op

revision = "007_catalog_active"
down_revision = "006_catalog_pricing_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.custom_catalogs') IS NOT NULL THEN
            ALTER TABLE custom_catalogs
              ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE custom_catalogs DROP COLUMN IF EXISTS active"
    )
