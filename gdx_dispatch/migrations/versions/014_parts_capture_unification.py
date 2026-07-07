"""Parts capture unification — source tag + sell price on job_parts_needed

PR4-billing-capture (2026-07-07). Parts capture was 4 write paths with only
ONE (parts-needed requests) able to reach an invoice: closeout JobPart rows,
mobile parts-used, and van usage were cost/inventory-only — a part a tech
logged at closeout was consumed, costed, and NEVER billed unless the office
re-keyed it by hand. The structural leak of the batch.

job_parts_needed becomes the single billable spine; the other paths insert
source-tagged rows into it (no fuzzy matching, no overwrites — every capture
event is its own row):

1. ``job_parts_needed.source`` — 'request' | 'closeout' | 'mobile' | 'van'.
   NOT NULL DEFAULT 'request' (every existing row IS a request).
2. ``job_parts_needed.unit_price`` — suggested SELL price (catalog
   Part.unit_price at capture time, not cost). NULL = office prices it.

job_parts_needed is NOT in baseline_squashed.sql (ORM-created, #41 ordering)
— guard with to_regclass so a fresh-DB boot no-ops (003 pattern).

Revision ID: 014_parts_capture_unification
Revises: 013_change_order_billing
"""
from alembic import op

revision = "014_parts_capture_unification"
down_revision = "013_change_order_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.job_parts_needed') IS NOT NULL THEN
            ALTER TABLE job_parts_needed
              ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'request';
            ALTER TABLE job_parts_needed
              ADD COLUMN IF NOT EXISTS unit_price NUMERIC(10,2);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.job_parts_needed') IS NOT NULL THEN
            ALTER TABLE job_parts_needed DROP COLUMN IF EXISTS source;
            ALTER TABLE job_parts_needed DROP COLUMN IF EXISTS unit_price;
          END IF;
        END $$;
        """
    )
