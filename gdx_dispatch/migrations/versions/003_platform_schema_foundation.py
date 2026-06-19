"""Platform schema foundation: identities, memberships, capabilities.

Revision ID: 003_platform_schema_foundation
Revises: 002_service_accounts
Create Date: 2026-04-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "003_platform_schema_foundation"
down_revision = "002_service_accounts"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [idx["name"] for idx in inspector.get_indexes(table_name)]
    return index_name in indexes


def _has_foreign_key(table_name: str, fk_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    fks = [fk.get("name") for fk in inspector.get_foreign_keys(table_name)]
    return fk_name in fks


def upgrade():
    if not _has_column("tenants", "parent_tenant_id"):
        op.add_column("tenants", sa.Column("parent_tenant_id", sa.String(100), nullable=True))
        op.create_foreign_key(
            "fk_tenants_parent_tenant_slug",
            "tenants",
            "tenants",
            ["parent_tenant_id"],
            ["slug"],
            ondelete="SET NULL",
        )
    if not _has_index("tenants", "ix_tenants_parent_tenant_id"):
        op.create_index("ix_tenants_parent_tenant_id", "tenants", ["parent_tenant_id"], unique=False)

    op.create_table(
        "identities",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_identities_email", "identities", ["email"], unique=False)
    op.create_index("ix_identities_status", "identities", ["status"], unique=False)

    op.create_table(
        "identity_providers",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("provider_subject", sa.String(255), nullable=False),
        sa.Column("provider_email", sa.String(255), nullable=True),
        sa.Column("email_verified_by_provider", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_authoritative_for_domain", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_type", "provider_subject", name="uq_idp_provider_subject"),
    )
    op.create_index("ix_idp_identity_id", "identity_providers", ["identity_id"], unique=False)
    op.create_index("ix_idp_provider_email", "identity_providers", ["provider_email"], unique=False)
    op.execute(
        """
        COMMENT ON COLUMN identity_providers.provider_email IS
          'sensitivity=restricted; PII; log-redact; backup-encrypt';
        COMMENT ON COLUMN identity_providers.metadata IS
          'sensitivity=restricted; may contain tokens or nested PII; log-redact; backup-encrypt';
        """
    )

    op.create_table(
        "capability_sets",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "scope_type", name="uq_capset_name_scope"),
    )

    op.create_table(
        "capabilities",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("capability_set_id", UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("instance_pattern", sa.String(255), nullable=False, server_default="*"),
        sa.Column("conditions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("parent_capability_id", UUID(as_uuid=True), nullable=True),
        sa.Column("granted_via_installation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["capability_set_id"], ["capability_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_capability_id"], ["capabilities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_capabilities_capset", "capabilities", ["capability_set_id"], unique=False)
    op.create_index("ix_capabilities_resource_type", "capabilities", ["resource_type"], unique=False)
    op.create_index("ix_capabilities_parent", "capabilities", ["parent_capability_id"], unique=False)
    op.create_index(
        "ix_capabilities_active",
        "capabilities",
        ["capability_set_id", "resource_type", "action"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "memberships",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("capability_set_id", UUID(as_uuid=True), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("granted_by_identity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["capability_set_id"], ["capability_sets.id"]),
        sa.ForeignKeyConstraint(["granted_by_identity_id"], ["identities.id"]),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.slug"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memberships_identity", "memberships", ["identity_id"], unique=False)
    op.create_index("ix_memberships_tenant", "memberships", ["tenant_id"], unique=False)
    op.create_index(
        "ix_memberships_tenant_active",
        "memberships",
        ["tenant_id", "identity_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "pending_invalidations",
        sa.Column("identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("identity_id"),
    )
    op.create_index(
        "ix_pending_invalidations_unreplayed",
        "pending_invalidations",
        ["enqueued_at"],
        unique=False,
        postgresql_where=sa.text("replayed_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_pending_invalidations_unreplayed", table_name="pending_invalidations")
    op.drop_table("pending_invalidations")

    op.drop_index("ix_memberships_tenant_active", table_name="memberships")
    op.drop_index("ix_memberships_tenant", table_name="memberships")
    op.drop_index("ix_memberships_identity", table_name="memberships")
    op.drop_table("memberships")

    op.drop_index("ix_capabilities_active", table_name="capabilities")
    op.drop_index("ix_capabilities_parent", table_name="capabilities")
    op.drop_index("ix_capabilities_resource_type", table_name="capabilities")
    op.drop_index("ix_capabilities_capset", table_name="capabilities")
    op.drop_table("capabilities")

    op.drop_table("capability_sets")

    op.drop_index("ix_idp_provider_email", table_name="identity_providers")
    op.drop_index("ix_idp_identity_id", table_name="identity_providers")
    op.drop_table("identity_providers")

    op.drop_index("ix_identities_status", table_name="identities")
    op.drop_index("ix_identities_email", table_name="identities")
    op.drop_table("identities")

    if _has_index("tenants", "ix_tenants_parent_tenant_id"):
        op.drop_index("ix_tenants_parent_tenant_id", table_name="tenants")
    if _has_foreign_key("tenants", "fk_tenants_parent_tenant_slug"):
        op.drop_constraint("fk_tenants_parent_tenant_slug", "tenants", type_="foreignkey")
    if _has_column("tenants", "parent_tenant_id"):
        op.drop_column("tenants", "parent_tenant_id")
