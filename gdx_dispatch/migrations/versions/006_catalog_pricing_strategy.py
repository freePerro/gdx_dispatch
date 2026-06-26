"""ADR-015 Slice 2 — per-catalog pricing strategy

Adds two columns to custom_catalogs:
  pricing_strategy  — strategy id ('manual' default = keep entered price)
  pricing_config    — declarative {kind, params} spec for pack-contributed pricing

Same situation as migration 005: custom_catalogs is built by create_all, not the
squashed baseline, so guard the ALTER with to_regclass (no-op on a fresh DB where
the table doesn't exist yet; runs on existing DBs). Idempotent for the
multi-container boot.

Revision ID: 006_catalog_pricing_strategy
Revises: 005_custom_catalog_fields
"""
from alembic import op

revision = "006_catalog_pricing_strategy"
down_revision = "005_custom_catalog_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.custom_catalogs') IS NOT NULL THEN
            ALTER TABLE custom_catalogs
              ADD COLUMN IF NOT EXISTS pricing_strategy VARCHAR(40) NOT NULL DEFAULT 'manual';
            ALTER TABLE custom_catalogs
              ADD COLUMN IF NOT EXISTS pricing_config JSON NOT NULL DEFAULT '{}';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE custom_catalogs DROP COLUMN IF EXISTS pricing_strategy"
    )
    op.get_bind().exec_driver_sql(
        "ALTER TABLE custom_catalogs DROP COLUMN IF EXISTS pricing_config"
    )
