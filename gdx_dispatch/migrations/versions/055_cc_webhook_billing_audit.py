"""add webhook_events and billing_audit_log tables

Revision ID: 055_cc_webhook_billing_audit
Revises: 054_cc_invoices_payments_connect
Create Date: 2026-05-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision = "055_cc_webhook_billing_audit"
revision = "055_cc_webhook_billing_audit"
down_revision = "054_cc_invoices_payments_connect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create webhook_events table
    op.create_table(
        "webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("stripe_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "endpoint",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "processing_status",
            sa.Text(),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "endpoint IN ('platform', 'connect')",
            name="chk_webhook_events_endpoint",
        ),
        sa.CheckConstraint(
            "processing_status IN ('queued', 'processing', 'complete', 'failed', 'dead_letter')",
            name="chk_webhook_events_status",
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="chk_webhook_events_retry_count",
        ),
        sa.UniqueConstraint("stripe_event_id", name="uq_webhook_events_stripe_event_id"),
    )

    # Create webhook_events indexes
    op.create_index(
        "ix_webhook_events_pending",
        "webhook_events",
        ["received_at"],
        postgresql_where=sa.text(
            "processing_status IN ('queued', 'failed')"
        ),
    )
    op.create_index(
        "ix_webhook_events_tenant_event_type",
        "webhook_events",
        ["tenant_id", "event_type", "received_at"],
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "ix_webhook_events_dead_letter",
        "webhook_events",
        ["received_at"],
        postgresql_where=sa.text("processing_status = 'dead_letter'"),
    )

    # RLS for webhook_events
    op.execute("ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_events FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY webhook_events_tenant_isolation ON webhook_events "
        "FOR ALL USING (tenant_id IS NOT NULL AND tenant_id = "
        "current_setting('app.tenant_id', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY webhook_events_cc_staff_read ON webhook_events "
        "FOR SELECT USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY webhook_events_cc_staff_write ON webhook_events "
        "FOR ALL USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
    )

    # Create billing_audit_log table
    op.create_table(
        "billing_audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "before_json",
            postgresql.JSONB(),
            server_default=sa.text("\'{}\'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "after_json",
            postgresql.JSONB(),
            server_default=sa.text("\'{}\'::jsonb"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("ip_inet", postgresql.INET(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            server_default=sa.text("\'{}\'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Create billing_audit_log indexes
    op.create_index(
        "ix_billing_audit_tenant_created",
        "billing_audit_log",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_billing_audit_actor_created",
        "billing_audit_log",
        ["actor_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_billing_audit_action",
        "billing_audit_log",
        ["action", sa.text("created_at DESC")],
    )

    # RLS for billing_audit_log
    op.execute("ALTER TABLE billing_audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_audit_log FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY billing_audit_log_tenant_read ON billing_audit_log "
        "FOR SELECT USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY billing_audit_log_cc_staff_read ON billing_audit_log "
        "FOR SELECT USING (current_setting('app.cc_staff_id', true) IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY billing_audit_log_cc_staff_insert ON billing_audit_log "
        "FOR INSERT WITH CHECK (actor_id::text = "
        "current_setting('app.cc_staff_id', true))"
    )


def downgrade() -> None:
    # Drop billing_audit_log policies and table
    op.execute("DROP POLICY IF EXISTS billing_audit_log_cc_staff_insert ON billing_audit_log")
    op.execute("DROP POLICY IF EXISTS billing_audit_log_cc_staff_read ON billing_audit_log")
    op.execute("DROP POLICY IF EXISTS billing_audit_log_tenant_read ON billing_audit_log")
    op.execute("ALTER TABLE billing_audit_log DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_billing_audit_action", table_name="billing_audit_log")
    op.drop_index("ix_billing_audit_actor_created", table_name="billing_audit_log")
    op.drop_index("ix_billing_audit_tenant_created", table_name="billing_audit_log")
    op.drop_table("billing_audit_log")

    # Drop webhook_events policies and table
    op.execute("DROP POLICY IF EXISTS webhook_events_cc_staff_write ON webhook_events")
    op.execute("DROP POLICY IF EXISTS webhook_events_cc_staff_read ON webhook_events")
    op.execute("DROP POLICY IF EXISTS webhook_events_tenant_isolation ON webhook_events")
    op.execute("ALTER TABLE webhook_events DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_webhook_events_dead_letter", table_name="webhook_events")
    op.drop_index("ix_webhook_events_tenant_event_type", table_name="webhook_events")
    op.drop_index("ix_webhook_events_pending", table_name="webhook_events")
    op.drop_table("webhook_events")


# Verification Manifest
# 1. Migrated tables: webhook_events, billing_audit_log
# 2. Check webhook_events: stripe_event_id is unique, status/endpoint/retry constraints present
# 3. Check billing_audit_log: tenant_id/actor_id FKs and JSONB defaults present
# 4. Check RLS: All 6 policies applied with correct current_setting usage
# 5. Check Downgrade: All policies, indexes, and tables dropped in correct order
