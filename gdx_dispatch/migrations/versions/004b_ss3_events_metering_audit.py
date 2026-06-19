"""SS-3b: events + metering + audit_logs ALTER.

Revision ID: 004b_ss3_events_metering_audit
Revises: 004a_ss3_oauth_surface
Create Date: 2026-04-14

Second of four chunked SS-3 migrations. Lands the immutability-guarded surfaces:
- event_outbox (D-39 immutable, DELETE allowed for retention cleanup)
- meter_events (D-39 immutable, no DELETE)
- audit_logs ALTER ADD installation_id, agent_identity, shared_via_resource_id, act_chain
  (audit_logs is created if missing — control DB doesn't have it pre-SS-3, but per-tenant
  DBs do; this migration is idempotent across both contexts.)

PG RULES enforce immutability at the DB layer (D-39). v3 patch P10 — RULES are
defense-in-depth; the real guarantee is least-privilege role separation.
GRANT/REVOKE for gdx_app role is wrapped in a DO block that no-ops if the role
is missing (so this migration runs cleanly in dev environments without that role).

Rollback boundary: chains to 004a. Reverting requires reverting 004c first if 3c's
shares.shared_via_resource_id FK has been wired (it isn't in 3b — 3c owns it).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "004b_ss3_events_metering_audit"
down_revision = "004a_ss3_oauth_surface"
branch_labels = None
depends_on = None


def _has_table(table_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name, column_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    cols = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in cols


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── event_outbox (Phase 2 schema, transport upgrade in SS-23) ───────────
    if not _has_table("event_outbox"):
        op.create_table(
            "event_outbox",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("event_name", sa.String(128), nullable=False),
            sa.Column("source_event_id", UUID(as_uuid=True), nullable=False, unique=True),
            sa.Column("tenant_id", sa.String(100), sa.ForeignKey("tenants.slug")),
            sa.Column("installation_id", UUID(as_uuid=True), sa.ForeignKey("installations.id")),
            sa.Column("payload", JSONB, nullable=False),
            sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            # IMMUTABLE per D-39 (no deleted_at; TTL truncation only — see retention cron)
        )
        op.create_index(
            "ix_outbox_undelivered", "event_outbox",
            ["emitted_at"],
            postgresql_where=sa.text("delivered_at IS NULL"),
        )

    # ── meter_events (D-36, D-39 immutable) ─────────────────────────────────
    if not _has_table("meter_events"):
        op.create_table(
            "meter_events",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("installation_id", UUID(as_uuid=True), sa.ForeignKey("installations.id"), nullable=False),
            sa.Column("billing_account_id", UUID(as_uuid=True), sa.ForeignKey("billing_accounts.id"), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("dimensions", JSONB, nullable=False, server_default="{}"),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index("ix_meter_events_install_time", "meter_events", ["installation_id", "occurred_at"])
        op.create_index("ix_meter_events_billing_time", "meter_events", ["billing_account_id", "occurred_at"])

    # ── audit_logs ─────────────────────────────────────────────────────────
    # Per spec: audit_logs is per-tenant existing. In control DB it doesn't exist
    # (verified production 2026-04-14). This migration handles both:
    #   - If audit_logs is missing, CREATE with platform columns from the start.
    #   - If it exists (per-tenant rollout), ALTER ADD.
    if not _has_table("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(100), sa.ForeignKey("tenants.slug")),
            sa.Column("user_id", sa.String(255)),
            sa.Column("action", sa.String(128), nullable=False),
            sa.Column("entity_type", sa.String(64)),
            sa.Column("entity_id", sa.String(255)),
            sa.Column("details", JSONB, nullable=False, server_default="{}"),
            sa.Column("ip_address", sa.String(45)),
            sa.Column("user_agent", sa.String(512)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("hash", sa.String(64)),
            sa.Column("prev_hash", sa.String(64)),
            # SS-3 platform columns:
            sa.Column("installation_id", UUID(as_uuid=True), sa.ForeignKey("installations.id"), nullable=True),
            sa.Column("agent_identity", sa.String(255), nullable=True),
            # shared_via_resource_id FK to shared_resources is wired in SS-3c.
            sa.Column("shared_via_resource_id", UUID(as_uuid=True), nullable=True),
            sa.Column("act_chain", JSONB, nullable=False, server_default="[]"),
        )
        op.create_index("ix_audit_install", "audit_logs", ["installation_id"])
        op.create_index("ix_audit_shared_via", "audit_logs", ["shared_via_resource_id"])
        op.create_index("ix_audit_tenant_time", "audit_logs", ["tenant_id", "created_at"])
    else:
        if not _has_column("audit_logs", "installation_id"):
            op.add_column("audit_logs", sa.Column("installation_id", UUID(as_uuid=True),
                          sa.ForeignKey("installations.id"), nullable=True))
            op.create_index("ix_audit_install", "audit_logs", ["installation_id"])
        if not _has_column("audit_logs", "agent_identity"):
            op.add_column("audit_logs", sa.Column("agent_identity", sa.String(255), nullable=True))
        if not _has_column("audit_logs", "shared_via_resource_id"):
            op.add_column("audit_logs", sa.Column("shared_via_resource_id", UUID(as_uuid=True), nullable=True))
            op.create_index("ix_audit_shared_via", "audit_logs", ["shared_via_resource_id"])
        if not _has_column("audit_logs", "act_chain"):
            op.add_column("audit_logs", sa.Column("act_chain", JSONB, nullable=False, server_default="[]"))

    # ── PG RULES (D-39 immutability defense-in-depth, v2 patch B4) ──────────
    if is_pg:
        op.execute("CREATE OR REPLACE RULE audit_logs_no_update AS ON UPDATE TO audit_logs DO INSTEAD NOTHING")
        op.execute("CREATE OR REPLACE RULE audit_logs_no_delete AS ON DELETE TO audit_logs DO INSTEAD NOTHING")
        op.execute("CREATE OR REPLACE RULE meter_events_no_update AS ON UPDATE TO meter_events DO INSTEAD NOTHING")
        op.execute("CREATE OR REPLACE RULE meter_events_no_delete AS ON DELETE TO meter_events DO INSTEAD NOTHING")
        op.execute("CREATE OR REPLACE RULE event_outbox_no_update AS ON UPDATE TO event_outbox DO INSTEAD NOTHING")
        # event_outbox DELETE is intentionally allowed (retention cron drops delivered rows >30 days).
        op.execute("CREATE OR REPLACE RULE oauth_client_keys_no_delete AS ON DELETE TO oauth_client_keys DO INSTEAD NOTHING")

        # v3 patch P10: GRANT/REVOKE for gdx_app role. Wrapped in DO block so it
        # no-ops if the role isn't present (dev environments often don't have it).
        op.execute("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gdx_app') THEN
                    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM gdx_app;
                    GRANT SELECT, INSERT ON audit_logs, meter_events, event_outbox TO gdx_app;
                    GRANT SELECT, INSERT, UPDATE ON oauth_client_keys TO gdx_app;
                END IF;
            END $$;
        """)

        # Sensitivity classification (v3 patch P11)
        op.execute("""
            COMMENT ON COLUMN meter_events.dimensions IS
                'sensitivity=internal; may reveal tenant usage patterns; log-redact in cross-tenant contexts';
        """)


def downgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("DROP RULE IF EXISTS oauth_client_keys_no_delete ON oauth_client_keys")
        op.execute("DROP RULE IF EXISTS event_outbox_no_update ON event_outbox")
        op.execute("DROP RULE IF EXISTS meter_events_no_delete ON meter_events")
        op.execute("DROP RULE IF EXISTS meter_events_no_update ON meter_events")
        op.execute("DROP RULE IF EXISTS audit_logs_no_delete ON audit_logs")
        op.execute("DROP RULE IF EXISTS audit_logs_no_update ON audit_logs")

    # We don't drop audit_logs in downgrade (it may have predated this migration on per-tenant DBs).
    # We only undo our column additions if they exist.
    if _has_column("audit_logs", "act_chain"):
        op.drop_column("audit_logs", "act_chain")
    if _has_column("audit_logs", "shared_via_resource_id"):
        op.drop_index("ix_audit_shared_via", table_name="audit_logs")
        op.drop_column("audit_logs", "shared_via_resource_id")
    if _has_column("audit_logs", "agent_identity"):
        op.drop_column("audit_logs", "agent_identity")
    if _has_column("audit_logs", "installation_id"):
        op.drop_index("ix_audit_install", table_name="audit_logs")
        op.drop_column("audit_logs", "installation_id")

    op.drop_table("meter_events")
    op.drop_table("event_outbox")
