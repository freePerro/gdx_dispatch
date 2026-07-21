"""Outlook vendor-bill history sweep — per-message ingest checkpoint

vendor-invoice-intake Phase 2, increment D3 (2026-07-21). The repeatable
history sweep walks the local ``outlook_messages`` mirror for allowlisted
senders' PDF attachments. ``vendor_bills_ingested_at`` is the checkpoint that
makes re-runs cheap: a stamped message is never re-downloaded. The partial
index serves the sweep's candidate scan (unprocessed + has attachments).

``outlook_messages`` is ORM-created (not in the squashed baseline), so on a
fresh DB create_orm_tables() already builds the column + index and this
migration is a guarded no-op. It only does real work on a DB where
outlook_messages already exists without the column.

Revision ID: 034_vendor_bill_sweep
Revises: 033_customer_user_password_hash
"""
from alembic import op

revision = "034_vendor_bill_sweep"
down_revision = "033_customer_user_password_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.outlook_messages') IS NOT NULL THEN
            ALTER TABLE outlook_messages
              ADD COLUMN IF NOT EXISTS vendor_bills_ingested_at timestamptz NULL;
            CREATE INDEX IF NOT EXISTS ix_email_vendor_bill_sweep
              ON outlook_messages (account_id, received_at)
              WHERE has_attachments AND vendor_bills_ingested_at IS NULL;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.outlook_messages') IS NOT NULL THEN
            DROP INDEX IF EXISTS ix_email_vendor_bill_sweep;
            ALTER TABLE outlook_messages DROP COLUMN IF EXISTS vendor_bills_ingested_at;
          END IF;
        END $$;
        """
    )
