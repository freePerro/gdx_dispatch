"""Closeout verification rails — plan §11 (audit A5/A6 resolutions).

Doug 2026-07-29: "have the office be called to verify the invoice. And have
it ask is this how many hours you meant? And have it ask how many techs on
site."

Three columns:

* job_closeouts.techs_on_site — crew size for the attested on-site duration.
  BILLING ONLY (billed man-hours = hours × techs under §8); it must never
  create payable hours for the other techs — their pay comes from their own
  day clock, and one tech attesting "there were two of us" is a billing
  fact, not a payroll attestation for a colleague (the standing
  labor-may-not-invent-hours rule). Default 1: every existing closeout was a
  one-tech attestation.

* invoices.verified_at / verified_by_user_id — the office's explicit
  approval. The mobile send endpoint refuses (409) while verified_at is
  NULL: a tech can CREATE an invoice from the truck but nothing reaches a
  customer until a second pair of eyes has seen the hours. This is the
  load-bearing half of A5 — POST /api/mobile/invoices/{id}/send already
  existed, so a fat-fingered 8 instead of 3 would have mailed an $800
  invoice with no review. Distinct from sent_at (a delivery fact) on
  purpose; do not overload either.

All adds are IF NOT EXISTS: create_orm_tables() runs before alembic on fresh
databases (docker/entrypoint.sh), so on those the columns already exist; on
every deployed environment this migration is what adds them (create_all
never alters existing tables — same split as 040/041).

Revision ID: 043_closeout_verification_rails
Revises: 042_job_type_canonicalize
"""
from alembic import op

revision = "043_closeout_verification_rails"
down_revision = "042_job_type_canonicalize"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE job_closeouts "
        "ADD COLUMN IF NOT EXISTS techs_on_site integer NOT NULL DEFAULT 1"
    )
    bind.exec_driver_sql(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS verified_at timestamptz"
    )
    bind.exec_driver_sql(
        "ALTER TABLE invoices "
        "ADD COLUMN IF NOT EXISTS verified_by_user_id varchar(36)"
    )

    # Backfill (audit round 2): every invoice the office already SENT is
    # de-facto verified — the office was the sender. Without this, mobile
    # re-send of any historical invoice 409s forever and the billing screen
    # paints the entire book (QB backfill included) amber "Unverified".
    # New drafts stay NULL, which is the population the gate exists for.
    bind.exec_driver_sql(
        "UPDATE invoices SET verified_at = sent_at, "
        "verified_by_user_id = 'migration:043_backfill' "
        "WHERE sent_at IS NOT NULL AND verified_at IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS verified_by_user_id")
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS verified_at")
    bind.exec_driver_sql("ALTER TABLE job_closeouts DROP COLUMN IF EXISTS techs_on_site")
