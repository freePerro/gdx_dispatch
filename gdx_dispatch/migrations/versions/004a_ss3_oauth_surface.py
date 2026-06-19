"""SS-3a: platform OAuth surface — installations + oauth_clients + oauth_client_keys + access_tokens + revocation_denylist.

Revision ID: 004a_ss3_oauth_surface
Revises: 003_platform_schema_foundation
Create Date: 2026-04-14

First of four chunked SS-3 migrations (per SS-3 spec v3 patch P12 — split for
blast-radius). This chunk lands the OAuth issuance + denylist surface plus the
deferred FK from SS-2 capabilities.granted_via_installation_id -> installations.id.

Rollback boundary: standalone. SS-3b/c/d depend on this chunk's tables for FKs.
Reverting this chunk requires reverting 3b first (audit_logs.installation_id FK).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "004a_ss3_oauth_surface"
down_revision = "003_platform_schema_foundation"
branch_labels = None
depends_on = None


def _has_table(table_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_foreign_key(table_name, fk_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    fks = [fk.get("name") for fk in inspector.get_foreign_keys(table_name)]
    return fk_name in fks


def upgrade():
    if not _has_table("oauth_clients"):
        op.create_table(
            "oauth_clients",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("client_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("owner_type", sa.String(32), nullable=False),
            sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
            sa.Column("redirect_uris", JSONB, nullable=False, server_default="[]"),
            sa.Column("scopes_requested", JSONB, nullable=False, server_default="[]"),
            sa.Column("client_type", sa.String(32), nullable=False),
            sa.Column("homepage_url", sa.String(512)),
            sa.Column("logo_url", sa.String(512)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("disabled_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_oauth_clients_owner", "oauth_clients", ["owner_type", "owner_id"])

    if not _has_table("oauth_client_keys"):
        op.create_table(
            "oauth_client_keys",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("oauth_client_id", UUID(as_uuid=True),
                      sa.ForeignKey("oauth_clients.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kid", sa.String(64), nullable=False),
            sa.Column("public_key_pem", sa.Text, nullable=False),
            sa.Column("state", sa.String(32), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("rotated_at", sa.DateTime(timezone=True)),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("oauth_client_id", "kid", name="uq_client_keys_kid"),
        )
        op.create_index(
            "ix_client_keys_active", "oauth_client_keys",
            ["oauth_client_id"],
            postgresql_where=sa.text("state = 'active'"),
        )

    # billing_accounts is canonical-defined in SS-3d; created here as a stub
    # so installations FK can land. SS-3d uses CREATE-IF-NOT-EXISTS to be safe.
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

    if not _has_table("installations"):
        op.create_table(
            "installations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("oauth_client_id", UUID(as_uuid=True),
                      sa.ForeignKey("oauth_clients.id"), nullable=False),
            sa.Column("tenant_id", sa.String(100),
                      sa.ForeignKey("tenants.slug", ondelete="CASCADE"), nullable=False),
            sa.Column("installer_identity_id", UUID(as_uuid=True),
                      sa.ForeignKey("identities.id"), nullable=False),
            sa.Column("capability_set_id", UUID(as_uuid=True),
                      sa.ForeignKey("capability_sets.id"), nullable=False),
            sa.Column("billing_account_id", UUID(as_uuid=True),
                      sa.ForeignKey("billing_accounts.id"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("uninstalled_at", sa.DateTime(timezone=True)),
            sa.Column("config", JSONB, nullable=False, server_default="{}"),
            sa.Column("health_status", sa.String(32), nullable=False, server_default="healthy"),
            sa.Column("last_event_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("oauth_client_id", "tenant_id", name="uq_install_app_tenant"),
        )
        op.create_index("ix_installations_tenant", "installations", ["tenant_id"])
        op.create_index("ix_installations_status", "installations", ["status"])

    if not _has_foreign_key("capabilities", "fk_capabilities_granted_via_install"):
        op.create_foreign_key(
            "fk_capabilities_granted_via_install",
            "capabilities", "installations",
            ["granted_via_installation_id"], ["id"],
            ondelete="CASCADE",
        )

    if not _has_table("access_tokens"):
        op.create_table(
            "access_tokens",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("prefix", sa.String(32), nullable=False),
            sa.Column("secret_hash", sa.String(255), nullable=False),
            sa.Column("owner_type", sa.String(32), nullable=False),
            sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
            sa.Column("installation_id", UUID(as_uuid=True),
                      sa.ForeignKey("installations.id"), nullable=True),
            sa.Column("capability_set_id", UUID(as_uuid=True),
                      sa.ForeignKey("capability_sets.id"), nullable=False),
            sa.Column("name", sa.String(128)),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("last_used_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.Column("key_version", sa.Integer, nullable=False, server_default="1"),
        )
        op.create_index(
            "ix_access_tokens_active", "access_tokens",
            ["prefix", "owner_type", "owner_id"],
            postgresql_where=sa.text("revoked_at IS NULL"),
        )
        op.create_index("ix_access_tokens_installation", "access_tokens", ["installation_id"])

    if not _has_table("revocation_denylist"):
        op.create_table(
            "revocation_denylist",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("token_jti", sa.String(64)),
            sa.Column("identity_id", UUID(as_uuid=True),
                      sa.ForeignKey("identities.id")),
            sa.Column("installation_id", UUID(as_uuid=True),
                      sa.ForeignKey("installations.id")),
            sa.Column("reason", sa.String(64), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_denylist_jti", "revocation_denylist", ["token_jti"])
        op.create_index("ix_denylist_identity", "revocation_denylist", ["identity_id"])
        op.create_index("ix_denylist_install", "revocation_denylist", ["installation_id"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("""
            COMMENT ON COLUMN access_tokens.secret_hash IS
                'sensitivity=restricted; one-way bcrypt hash of issued token; log-redact';
            COMMENT ON COLUMN installations.config IS
                'sensitivity=restricted; per-install settings may include API keys; log-redact unless explicit subfield downgrade';
            COMMENT ON COLUMN oauth_client_keys.public_key_pem IS
                'sensitivity=internal; not a secret but identifies specific installations; restrict analytics reads';
        """)


def downgrade():
    if _has_foreign_key("capabilities", "fk_capabilities_granted_via_install"):
        op.drop_constraint("fk_capabilities_granted_via_install", "capabilities", type_="foreignkey")
    op.drop_table("revocation_denylist")
    op.drop_table("access_tokens")
    op.drop_table("installations")
    op.drop_table("billing_accounts")
    op.drop_table("oauth_client_keys")
    op.drop_table("oauth_clients")
