"""notifications.deleted_at + phone_com_messages.read_at

Two per-row state columns for 2026-07-24 UX gaps:

- notifications.deleted_at (text ISO, matches the model's created_at style):
  the bell drawer finally gets delete / clear-all. Soft delete, house
  pattern — count/list/mark-read filter deleted_at IS NULL.
- phone_com_messages.read_at (timestamptz, mirrors voicemail heard_at):
  local read marker for inbound SMS. NULL on an inbound row = unread;
  drives GET /api/phone-com/messages/unread-count (sidebar badge) and the
  per-thread unread counts. Stamped when a thread is opened or explicitly
  marked read.

Both tables are created by SQLAlchemy create_all() at app startup, NOT in
baseline_squashed.sql — same fresh-DB ordering guard as 036: no-op before
the table exists (create_all builds it WITH the column from the model).
IF NOT EXISTS keeps it idempotent across multi-container boots.

Revision ID: 037_notification_delete_sms_read
Revises: 036_invoice_estimate_id
"""
from alembic import op

revision = "037_notification_delete_sms_read"
down_revision = "036_invoice_estimate_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.notifications') IS NOT NULL THEN
            ALTER TABLE notifications ADD COLUMN IF NOT EXISTS deleted_at text;
          END IF;
          IF to_regclass('public.phone_com_messages') IS NOT NULL THEN
            ALTER TABLE phone_com_messages
              ADD COLUMN IF NOT EXISTS read_at timestamptz;
            -- Backfill: stamp every PRE-EXISTING inbound message read.
            -- Without this, deploy day floods the new badge with the entire
            -- SMS history (prod carries 128 backfilled messages) — "unread"
            -- must mean "arrived after the feature existed and nobody
            -- opened the thread", not "predates the read marker".
            UPDATE phone_com_messages
              SET read_at = COALESCE(created_at, now())
              WHERE direction = 'in' AND read_at IS NULL;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE notifications DROP COLUMN IF EXISTS deleted_at"
    )
    op.get_bind().exec_driver_sql(
        "ALTER TABLE phone_com_messages DROP COLUMN IF EXISTS read_at"
    )
