"""ADR-015 — no-code custom catalog types: field_schema + attributes JSON

Adds two JSON columns:
  custom_catalogs.field_schema  — ordered field definitions for product_class='custom'
  custom_catalog_items.attributes — values for those user-defined fields

Both tables are created by SQLAlchemy create_all() at app startup, NOT by the
squashed baseline (see migration 003 for the same situation). On a fresh DB the
entrypoint runs `alembic upgrade head` BEFORE the app boots, so the tables don't
exist yet — guard each ALTER with to_regclass so the migration is a no-op then
(create_all builds the tables WITH these columns from the model). On existing DBs
the tables are present and the ALTERs run. IF NOT EXISTS keeps it idempotent
across the multi-container boot (every container runs upgrade head).

Revision ID: 005_custom_catalog_fields
Revises: 004_backfill_pricing_category
"""
from alembic import op

revision = "005_custom_catalog_fields"
down_revision = "004_backfill_pricing_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.custom_catalogs') IS NOT NULL THEN
            ALTER TABLE custom_catalogs
              ADD COLUMN IF NOT EXISTS field_schema JSON NOT NULL DEFAULT '[]';
          END IF;
          IF to_regclass('public.custom_catalog_items') IS NOT NULL THEN
            ALTER TABLE custom_catalog_items
              ADD COLUMN IF NOT EXISTS attributes JSON NOT NULL DEFAULT '{}';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE custom_catalogs DROP COLUMN IF EXISTS field_schema"
    )
    op.get_bind().exec_driver_sql(
        "ALTER TABLE custom_catalog_items DROP COLUMN IF EXISTS attributes"
    )
