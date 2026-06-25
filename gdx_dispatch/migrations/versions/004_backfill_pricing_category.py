"""backfill custom_catalog_items.pricing_category

Catalog items created via add/import before this change never had
pricing_category written (only save-from-estimate-line did). A NULL bucket
makes the estimate tier engine skip markup, so those items priced at cost
(zero margin) on estimates. This derives a bucket from the item's free-form
category / product_class for every existing row, mirroring
catalog._derive_pricing_category, so the update catches them on deploy.

Idempotent: only touches rows where pricing_category IS NULL, so it's safe
under the entrypoint's "alembic upgrade head" on every boot.

Revision ID: 004_backfill_pricing_category
Revises: 003_estimate_line_metadata
"""
from alembic import op

revision = "004_backfill_pricing_category"
down_revision = "003_estimate_line_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # custom_catalog_items is NOT in baseline_squashed.sql — created by
    # create_all() at app startup, which on a fresh DB runs AFTER the
    # entrypoint's `alembic upgrade head`. Guard the backfill so it no-ops when
    # the table doesn't exist yet (a fresh DB has nothing to backfill anyway);
    # on existing DBs it runs. Mirrors the guard in migration 003.
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.custom_catalog_items') IS NULL THEN
            RETURN;
          END IF;
        UPDATE custom_catalog_items
        SET pricing_category = CASE
          WHEN lower(trim(category)) IN ('doors','openers','parts','other') THEN lower(trim(category))
          WHEN lower(trim(category)) IN ('springs','spring','remote','remotes','keypad','keypads',
                                         'accessory','accessories','hardware','track','tracks',
                                         'cable','cables','part') THEN 'parts'
          WHEN lower(trim(category)) IN ('opener','operator','operators') THEN 'openers'
          WHEN lower(trim(category)) = 'door' THEN 'doors'
          WHEN lower(trim(product_class)) = 'door' THEN 'doors'
          WHEN lower(trim(product_class)) = 'opener' THEN 'openers'
          WHEN lower(trim(product_class)) IN ('spring','track','remote','parts') THEN 'parts'
          WHEN lower(trim(product_class)) = 'labor' THEN NULL
          ELSE 'other'
        END
        WHERE pricing_category IS NULL AND deleted_at IS NULL;
        END $$;
        """
    )


def downgrade() -> None:
    # No-op: the original NULLs aren't recoverable and re-nulling would
    # re-introduce the zero-margin bug. Leaving the derived values in place.
    pass
