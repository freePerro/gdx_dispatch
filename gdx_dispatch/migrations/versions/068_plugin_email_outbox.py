"""Plugin email outbox — plugins get full, auditable email access.

Email overhaul Phase 6 (docs/design/email-readability-and-delivery-plan.md,
locked with Doug 2026-08-18: "full access for plugins", conditional on
auditability). The plugin-host container has no egress, so plugins queue
email here; the core drain sends via send_transactional_email (Outlook/SMTP,
outbound_emails audit row, designated sender). Consent-gated on the new
"email" permission at drain time.

052 pattern: ORM-registered model, CREATE TABLE IF NOT EXISTS, idempotent.

Revision ID: 068_plugin_email_outbox
Revises: 067_automation_email_option
"""
from alembic import op

revision = "068_plugin_email_outbox"
down_revision = "067_automation_email_option"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS plugin_email_outbox (
            id            uuid PRIMARY KEY,
            company_id    varchar(36) NOT NULL,
            plugin_key    varchar(80) NOT NULL,
            delivery_id   varchar(80) NOT NULL,
            to_email      text,
            customer_id   varchar(36),
            contact_id    varchar(36),
            subject       text NOT NULL,
            body_text     text,
            body_html     text,
            entity_type   varchar(30),
            entity_id     varchar(64),
            status        varchar(12) NOT NULL DEFAULT 'queued',
            attempts      integer NOT NULL DEFAULT 0,
            last_error    varchar(120),
            created_at    timestamptz NOT NULL DEFAULT now(),
            processed_at  timestamptz
        );
        """
    )
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_plugin_email_outbox_delivery "
        "ON plugin_email_outbox (plugin_key, delivery_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_plugin_email_outbox_status ON plugin_email_outbox (status);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_plugin_email_outbox_company_id ON plugin_email_outbox (company_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_plugin_email_outbox_plugin_key ON plugin_email_outbox (plugin_key);"
    )


def downgrade() -> None:
    # Queue + audit-adjacent — keep on downgrade.
    pass
