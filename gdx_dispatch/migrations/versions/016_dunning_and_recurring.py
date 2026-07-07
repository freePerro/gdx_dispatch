"""Dunning opt-in + reminder idempotency + per-invoice mute

PR6-billing-capture (2026-07-07). Doug's decisions:
- Automated dunning is OPT-IN, default OFF (`auto_send_enabled`); while off
  a weekly nudge tells admin/owner what isn't being chased, permanently
  dismissible (`auto_send_nudge_dismissed`).
- Automated sends record WHICH threshold fired (`threshold_days`) — the
  idempotency key that survives schedule edits; manual logs (NULL) never
  suppress the robot.
- `invoices.dunning_paused` — the explicit per-invoice mute for real
  payment arrangements.

All three tables are ORM-created (#41 ordering) — to_regclass-guarded.

Revision ID: 016_dunning_and_recurring
Revises: 015_completion_gates
"""
from alembic import op

revision = "016_dunning_and_recurring"
down_revision = "015_completion_gates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.reminder_settings') IS NOT NULL THEN
            ALTER TABLE reminder_settings
              ADD COLUMN IF NOT EXISTS auto_send_enabled BOOLEAN NOT NULL DEFAULT false;
            ALTER TABLE reminder_settings
              ADD COLUMN IF NOT EXISTS auto_send_nudge_dismissed BOOLEAN NOT NULL DEFAULT false;
          END IF;
          IF to_regclass('public.payment_reminders') IS NOT NULL THEN
            ALTER TABLE payment_reminders
              ADD COLUMN IF NOT EXISTS threshold_days INTEGER;
          END IF;
          IF to_regclass('public.invoices') IS NOT NULL THEN
            ALTER TABLE invoices
              ADD COLUMN IF NOT EXISTS dunning_paused BOOLEAN NOT NULL DEFAULT false;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.reminder_settings') IS NOT NULL THEN
            ALTER TABLE reminder_settings DROP COLUMN IF EXISTS auto_send_enabled;
            ALTER TABLE reminder_settings DROP COLUMN IF EXISTS auto_send_nudge_dismissed;
          END IF;
          IF to_regclass('public.payment_reminders') IS NOT NULL THEN
            ALTER TABLE payment_reminders DROP COLUMN IF EXISTS threshold_days;
          END IF;
          IF to_regclass('public.invoices') IS NOT NULL THEN
            ALTER TABLE invoices DROP COLUMN IF EXISTS dunning_paused;
          END IF;
        END $$;
        """
    )
