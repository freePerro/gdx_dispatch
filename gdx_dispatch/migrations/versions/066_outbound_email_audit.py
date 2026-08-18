"""Outbound email audit trail + recipient/channel columns.

Email overhaul (docs/design/email-readability-and-delivery-plan.md), locked
requirement: every send attempt must be auditable — who/what triggered it,
the exact rendered body, who it went to, what happened.

Three pieces, all idempotent:

1. ``outbound_emails`` — CREATE TABLE IF NOT EXISTS (052 pattern: the model
   is ORM-registered, so create_orm_tables() may or may not have built it
   first; either order is a no-op). Append-only; written inside
   send_transactional_email on its own session.

2. ``customer_contacts.is_primary`` — the default person automated sends
   (one-click, bulk, reminders, receipts, workflow rules, plugins) greet and
   address on a business account. Without it those paths can only ever say
   "Hi <Company Name>". Composer picks explicitly; this is the no-human
   default.

3. ``estimates.sent_via`` — estimates could record WHEN they were sent but
   not HOW (the audit blob hardcoded "manual" for every channel). Mirrors
   invoices.sent_via; NULL on rows sent before this migration.

Revision ID: 066_outbound_email_audit
Revises: 065_vendor_bill_payments
"""
import contextlib

from alembic import op

revision = "066_outbound_email_audit"
down_revision = "065_vendor_bill_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if not is_pg:
        # SQLite lane (CLAUDE.md: every migration runs on both engines).
        # Same objects, portable syntax: CURRENT_TIMESTAMP default, no DO $$
        # (column-adds are try/except — SQLite lacks ADD COLUMN IF NOT
        # EXISTS), text-affinity types.
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS outbound_emails (
                id text PRIMARY KEY, company_id varchar(36) NOT NULL,
                initiator_kind varchar(20) NOT NULL DEFAULT 'user',
                kind varchar(20), initiator_ref varchar(120),
                entity_type varchar(30), entity_id varchar(64),
                to_email text NOT NULL, to_name text,
                recipient_source varchar(20), recipient_contact_id varchar(36),
                subject text NOT NULL, body_html text NOT NULL,
                attachments_meta json, provider varchar(20),
                status varchar(12) NOT NULL DEFAULT 'failed',
                skip_reason varchar(60), bounced_at timestamp,
                created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for idx in (
            "CREATE INDEX IF NOT EXISTS ix_outbound_emails_company_id ON outbound_emails (company_id);",
            "CREATE INDEX IF NOT EXISTS ix_outbound_emails_created_at ON outbound_emails (created_at);",
            "CREATE INDEX IF NOT EXISTS ix_outbound_emails_entity ON outbound_emails (entity_type, entity_id);",
        ):
            bind.exec_driver_sql(idx)
        for alter in (
            "ALTER TABLE customer_contacts ADD COLUMN is_primary boolean NOT NULL DEFAULT false;",
            "ALTER TABLE estimates ADD COLUMN sent_via varchar(20);",
        ):
            # Table absent (boot creates it with the column) or column exists.
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(alter)
        return
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS outbound_emails (
            id                    uuid PRIMARY KEY,
            company_id            varchar(36) NOT NULL,
            initiator_kind        varchar(20) NOT NULL DEFAULT 'user',
            kind                  varchar(20),
            initiator_ref         varchar(120),
            entity_type           varchar(30),
            entity_id             varchar(64),
            to_email              text NOT NULL,
            to_name               text,
            recipient_source      varchar(20),
            recipient_contact_id  varchar(36),
            subject               text NOT NULL,
            body_html             text NOT NULL,
            attachments_meta      json,
            provider              varchar(20),
            status                varchar(12) NOT NULL DEFAULT 'failed',
            skip_reason           varchar(60),
            bounced_at            timestamptz,
            created_at            timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_outbound_emails_company_id ON outbound_emails (company_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_outbound_emails_created_at ON outbound_emails (created_at);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_outbound_emails_entity ON outbound_emails (entity_type, entity_id);"
    )
    # customer_contacts is a create_orm_tables() boot table (not in the SQL
    # baseline) — on a fresh DB where migrations run first, the table doesn't
    # exist yet and boot will create it WITH is_primary, so skipping is
    # correct; on an existing DB the ALTER adds the column.
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
            IF to_regclass('customer_contacts') IS NOT NULL THEN
                ALTER TABLE customer_contacts
                    ADD COLUMN IF NOT EXISTS is_primary boolean NOT NULL DEFAULT false;
            END IF;
        END $$;
        """
    )
    # estimates is likewise boot-created (003/013 pattern): fresh DB → the
    # model already carries sent_via; existing DB → ALTER adds it.
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
            IF to_regclass('estimates') IS NOT NULL THEN
                ALTER TABLE estimates ADD COLUMN IF NOT EXISTS sent_via varchar(20);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Append-only audit data — never drop on downgrade. Columns stay too
    # (both are additive and NULL/false-safe for older code).
    pass
