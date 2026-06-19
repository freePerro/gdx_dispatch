"""SS-3d: platform supporting surfaces — developer_accounts, billing_accounts (canonical), sandbox_envs, sso_configs.

Revision ID: 004d_ss3_supporting
Revises: 004c_ss3_sharing
Create Date: 2026-04-14

Fourth and final SS-3 migration. Lands platform-internal surfaces that have no
inter-group FKs (per SS-3 spec — supporting tables can land last).

Note on billing_accounts: SS-3a creates a stub for the installations FK to land.
This migration is the canonical owner — uses CREATE-IF-NOT-EXISTS so the stub
isn't disturbed. If you ever want to evolve billing_accounts schema after first
ship, do it in a separate migration that ALTERs explicitly.

Rollback boundary: chains to 004c. Pure leaf — nothing depends on 3d's tables.
Reverting 3d alone is safe.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "004d_ss3_supporting"
down_revision = "004c_ss3_sharing"
branch_labels = None
depends_on = None


def _has_table(table_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade():
    if not _has_table("developer_accounts"):
        op.create_table(
            "developer_accounts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("email", sa.String(255), nullable=False, unique=True),
            sa.Column("display_name", sa.String(255)),
            sa.Column("password_hash", sa.String(255)),
            sa.Column("email_verified_at", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    # billing_accounts: SS-3a created a stub. We only do anything here if 3a's
    # IF-NOT-EXISTS branch was somehow skipped. Idempotent.
    if not _has_table("billing_accounts"):
        op.create_table(
            "billing_accounts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("owner_type", sa.String(32), nullable=False),
            sa.Column("owner_id", UUID(as_uuid=True)),
            sa.Column("stripe_customer_id", sa.String(64)),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("payment_method_id", sa.String(64)),
            sa.Column("invoice_email", sa.String(255)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("suspended_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_billing_owner", "billing_accounts", ["owner_type", "owner_id"])

    if not _has_table("sandbox_envs"):
        op.create_table(
            "sandbox_envs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(100), sa.ForeignKey("tenants.slug"), nullable=False),
            sa.Column("subdomain", sa.String(128), nullable=False, unique=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="provisioning"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("last_reset_at", sa.DateTime(timezone=True)),
            sa.Column("torn_down_at", sa.DateTime(timezone=True)),
        )

    if not _has_table("sso_configs"):
        op.create_table(
            "sso_configs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(100), sa.ForeignKey("tenants.slug"), nullable=False),
            sa.Column("federation_mode", sa.String(32), nullable=False, server_default="hybrid"),
            sa.Column("provider_type", sa.String(32), nullable=False),
            sa.Column("provider_metadata", JSONB, nullable=False),
            sa.Column("authoritative_domains", JSONB, nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("disabled_at", sa.DateTime(timezone=True)),
        )

    # Sensitivity classification (v3 patch P11)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("""
            COMMENT ON COLUMN sso_configs.provider_metadata IS
                'sensitivity=restricted; contains SAML certs and IdP secrets; log-redact; backup-encrypt';
            COMMENT ON COLUMN developer_accounts.email IS
                'sensitivity=restricted; PII; log-redact; backup-encrypt';
        """)


def downgrade():
    op.drop_table("sso_configs")
    op.drop_table("sandbox_envs")
    # We don't drop billing_accounts in downgrade — it was created by 3a (stub)
    # and cleaning it up belongs to 3a's downgrade.
    op.drop_table("developer_accounts")
