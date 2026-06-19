"""SS-28 — platform_consumer_audit + audit_retention_policy.

INTEGRATION_TODO: chained on placeholder ``down_revision = "ss27_cross_tenant_sharing"``.
The supervisor will retarget this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - platform_consumer_audit (append-only; hash-chained per tenant)
    - audit_retention_policy   (per-tenant retention window)

Both tables are NEW. This migration does NOT touch any pre-existing
audit_event / audit_logs table — those belong to separate surfaces and
SS-28 is explicitly additive per the sprint plan "DO NOT TOUCH" list.

Revision ID: ss28_audit
Down revision: INTEGRATION_TODO
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers.
revision = "ss28_audit"
down_revision = "ss27_cross_tenant_sharing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_consumer_audit",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("principal_identity_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "ix_sca_tenant_created",
        "platform_consumer_audit",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_sca_principal",
        "platform_consumer_audit",
        ["principal_identity_id"],
    )
    op.create_index(
        "ix_sca_action",
        "platform_consumer_audit",
        ["action"],
    )
    op.create_index(
        "ix_sca_resource",
        "platform_consumer_audit",
        ["resource_type", "resource_id"],
    )

    op.create_table(
        "audit_retention_policy",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_arp_tenant"),
    )


def downgrade() -> None:
    op.drop_table("audit_retention_policy")
    op.drop_index("ix_sca_resource", table_name="platform_consumer_audit")
    op.drop_index("ix_sca_action", table_name="platform_consumer_audit")
    op.drop_index("ix_sca_principal", table_name="platform_consumer_audit")
    op.drop_index("ix_sca_tenant_created", table_name="platform_consumer_audit")
    op.drop_table("platform_consumer_audit")
