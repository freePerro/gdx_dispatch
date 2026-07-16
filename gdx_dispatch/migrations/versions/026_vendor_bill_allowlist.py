"""Outlook vendor-bill auto-ingest — sender allowlist column

vendor-invoice-intake Phase 2 (2026-07-16). The Outlook delta sync can now
auto-ingest a supplier's PDF bill attachments into the vendor-bills review
queue. It is opt-in per tenant via a sender allowlist; empty = feature off.

``outlook_settings`` is ORM-created (not in the squashed baseline), so on a
fresh DB create_orm_tables() already builds the column and this migration is a
guarded no-op. It only does real work on a DB where outlook_settings already
exists without the column.

Revision ID: 026_vendor_bill_allowlist
Revises: 025_vendor_invoice_dedup_index

(Renamed from 026_outlook_vendor_bill_allowlist: alembic_version.version_num
is varchar(32) and the original ID was 33 chars. The rename is safe because
Postgres RAISES on an over-length varchar — it never truncates-and-stores —
so no DB anywhere can hold the old ID; and env.py's single-transaction
upgrade rolled this migration's DDL back with the failed stamp, so no DB is
left half-applied either. Re-running is harmless regardless: the upgrade is
IF-guarded and idempotent.)
"""
from alembic import op

revision = "026_vendor_bill_allowlist"
down_revision = "025_vendor_invoice_dedup_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.outlook_settings') IS NOT NULL THEN
            ALTER TABLE outlook_settings
              ADD COLUMN IF NOT EXISTS vendor_bill_sender_allowlist JSONB NOT NULL DEFAULT '[]'::jsonb;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.outlook_settings') IS NOT NULL THEN
            ALTER TABLE outlook_settings DROP COLUMN IF EXISTS vendor_bill_sender_allowlist;
          END IF;
        END $$;
        """
    )
