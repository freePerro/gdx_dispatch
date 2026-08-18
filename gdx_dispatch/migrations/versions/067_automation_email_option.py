"""Automation emails as an on/off option + designated sender.

Email overhaul Phase 4a (docs/design/email-readability-and-delivery-plan.md,
locked with Doug 2026-08-18): the workflow engine's send_email action becomes
real, gated behind a tenant toggle.

- ``app_settings.automation_emails_enabled`` — default FALSE, and that default
  is load-bearing: every workflow action has been a logged no-op forever, so
  any is_active rule configured in the past with a send_email action must not
  start emailing customers the moment this deploys. Turning it on is a
  deliberate act in Settings.
- ``app_settings.automation_sender_user_id`` — whose Outlook connection
  automated emails send as. Background sends have no calling user, which
  skips the Graph path; on a tenant with no SMTP row (prod) that would mean
  silent non-delivery even with the toggle ON.

Revision ID: 067_automation_email_option
Revises: 066_outbound_email_audit
"""
from alembic import op

revision = "067_automation_email_option"
down_revision = "066_outbound_email_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS "
        "automation_emails_enabled boolean NOT NULL DEFAULT false;"
    )
    bind.exec_driver_sql(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS "
        "automation_sender_user_id varchar(36);"
    )
    # Phase 5.7 — optional Reply-To for SMTP sends (Graph sends already
    # thread to the sending rep's mailbox).
    bind.exec_driver_sql(
        "ALTER TABLE email_settings ADD COLUMN IF NOT EXISTS "
        "reply_to_email varchar(254);"
    )


def downgrade() -> None:
    # Additive, false/NULL-safe for older code — keep on downgrade.
    pass
