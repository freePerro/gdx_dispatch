"""Widen job_parts_needed.sku to 255 — the picker can offer skus it can't store

``job_parts_needed.sku`` was varchar(64) while every catalog the part picker
searches allows more: chi_parts_catalog.sku and chi_door_catalog.sku are
varchar(255), custom_catalog_items.sku is varchar(100). Since sku-suggest
started searching those catalogs (2026-07-16) the picker can hand the tech a
sku the request table cannot hold, and the POST 422s — loudly online, and
SILENTLY offline, where the queued write is marked failed and the part request
is simply gone.

No live risk when written: the longest real sku across all three catalogs is 39
characters. This is closing the gap before a supplier import lands a longer one,
not fixing a fire.

Widening is the fix rather than truncating in the picker: a truncated sku is not
a shorter sku, it is a **different part**. Trimming it would have dispatch order
the wrong thing, which is worse than the 422 it prevents.

255 matches the widest source (the CHI catalogs). varchar widening is a metadata
change in Postgres — no table rewrite, no lock beyond a brief ACCESS EXCLUSIVE,
and no data can be lost since every existing value already fits in 64.

``job_parts_needed`` is ORM-created (not in the squashed baseline), so on a
fresh DB create_orm_tables() builds sku at its model width and this is a guarded
no-op. Idempotent: it checks the current width first.

Revision ID: 028_parts_needed_sku_255
Revises: 027_job_note_author_backfill
"""
from alembic import op

revision = "028_parts_needed_sku_255"
down_revision = "027_job_note_author_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'job_parts_needed'
              AND column_name = 'sku'
              AND character_maximum_length IS NOT NULL
              AND character_maximum_length < 255
          ) THEN
            ALTER TABLE job_parts_needed ALTER COLUMN sku TYPE varchar(255);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Narrowing back to 64 would RAISE on any row that has since stored a
    # longer sku (Postgres never truncates-and-stores), so this "reversal"
    # could fail mid-flight or, worse, be run against data it would reject.
    # The forward change is loss-free; leave it applied.
    pass
