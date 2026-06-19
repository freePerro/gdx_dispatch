"""SS-3c: platform sharing — shared_resources, shares, resource_descriptors, resource_field_descriptors.

Revision ID: 004c_ss3_sharing
Revises: 004b_ss3_events_metering_audit
Create Date: 2026-04-14

Third of four chunked SS-3 migrations. Lands the cross-tenant sharing surface
(D-34) plus the resource-descriptor extensibility surface (D-38).

Also wires the FK from audit_logs.shared_via_resource_id -> shared_resources.id
(SS-3b created the column without the FK because shared_resources didn't exist yet).

Notes on shared_resources.resource_id (v2 patch O3 + v3 patch P13):
- TEXT, not UUID. GDX has mixed PK types (UUID, slug, int).
- No FK constraint by design — resource_type tells us which target table.
- Compensating controls: app-layer existence check + nightly orphan sweep + per-type test.

Rollback boundary: chains to 004b. SS-3d has no FKs to 3c so safe to revert
3c independently of 3d.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "004c_ss3_sharing"
down_revision = "004b_ss3_events_metering_audit"
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
    if not _has_table("resource_descriptors"):
        op.create_table(
            "resource_descriptors",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("resource_type", sa.String(128), nullable=False, unique=True),
            sa.Column("owner", sa.String(128), nullable=False),
            sa.Column("schema", JSONB, nullable=False),
            sa.Column("capabilities_supported", JSONB, nullable=False, server_default="[]"),
            sa.Column("introspection_endpoint", sa.String(512)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    if not _has_table("resource_field_descriptors"):
        op.create_table(
            "resource_field_descriptors",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("resource_descriptor_id", UUID(as_uuid=True),
                      sa.ForeignKey("resource_descriptors.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", sa.String(100), sa.ForeignKey("tenants.slug")),
            sa.Column("field_name", sa.String(128), nullable=False),
            sa.Column("field_type", sa.String(32), nullable=False),
            sa.Column("sensitivity_classification", sa.String(32), nullable=False, server_default="internal"),
            sa.Column("description", sa.Text),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("resource_descriptor_id", "tenant_id", "field_name", name="uq_field_descriptor"),
        )

    if not _has_table("shared_resources"):
        op.create_table(
            "shared_resources",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("owner_tenant_id", sa.String(100), sa.ForeignKey("tenants.slug"), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=False),
            sa.Column("resource_id", sa.Text, nullable=False),
            sa.Column("shared_via_installation_id", UUID(as_uuid=True), sa.ForeignKey("installations.id")),
            sa.Column("visibility", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_shared_resources_owner", "shared_resources", ["owner_tenant_id"])
        op.create_index("ix_shared_resources_resource", "shared_resources", ["resource_type", "resource_id"])

    if not _has_table("shares"):
        op.create_table(
            "shares",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("shared_resource_id", UUID(as_uuid=True),
                      sa.ForeignKey("shared_resources.id", ondelete="CASCADE"), nullable=False),
            sa.Column("target_tenant_id", sa.String(100), sa.ForeignKey("tenants.slug")),
            sa.Column("target_installation_id", UUID(as_uuid=True), sa.ForeignKey("installations.id")),
            sa.Column("capabilities", JSONB, nullable=False, server_default="[]"),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_shares_target_tenant", "shares", ["target_tenant_id"])
        op.create_index("ix_shares_target_install", "shares", ["target_installation_id"])

    # Wire the deferred FK from audit_logs.shared_via_resource_id (SS-3b created
    # the column without the FK because shared_resources didn't exist yet).
    if _has_table("audit_logs") and not _has_foreign_key("audit_logs", "fk_audit_shared_via_resource"):
        op.create_foreign_key(
            "fk_audit_shared_via_resource",
            "audit_logs", "shared_resources",
            ["shared_via_resource_id"], ["id"],
        )


def downgrade():
    if _has_foreign_key("audit_logs", "fk_audit_shared_via_resource"):
        op.drop_constraint("fk_audit_shared_via_resource", "audit_logs", type_="foreignkey")
    op.drop_table("shares")
    op.drop_table("shared_resources")
    op.drop_table("resource_field_descriptors")
    op.drop_table("resource_descriptors")
