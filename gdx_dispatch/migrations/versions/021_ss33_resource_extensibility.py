"""SS-33 resource extensibility tables.

Revision ID: ss33_resource_extensibility
Revises: INTEGRATION_TODO
Create Date: 2026-04-19

INTEGRATION_TODO:
    - set ``down_revision`` to the actual latest revision in the main
      chain once SS-33 integration lands and SS33Base is merged onto
      the primary platform Base.
    - rename this file to the next sequential number at that time.
    - remove the ``INTEGRATION_TODO`` placeholder and mount routers
      (``resource_types``, ``resource_instances``) in ``gdx_dispatch/main.py``.
    - wire ``gdx_dispatch.core.resource_type_loader.bootstrap(session)`` into
      the app startup hook after DB init.

Creates:
    - resource_type                    (tenant-private type declarations)
    - resource_instance                (generic per-type data rows)
    - resource_type_deletion_request   (7-day grace-period tracker)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ss33_resource_extensibility"
down_revision = "ss32_spiffe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resource_type",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("json_schema", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("index_hints", sa.JSON(), nullable=False),
        sa.Column("owner_tenant_id", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_resource_type_name"),
    )
    op.create_index(
        "ix_resource_type_owner", "resource_type", ["owner_tenant_id"]
    )

    op.create_table(
        "resource_instance",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("type_name", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_resource_instance_tenant_type",
        "resource_instance",
        ["tenant_id", "type_name"],
    )
    op.create_index(
        "ix_resource_instance_type", "resource_instance", ["type_name"]
    )

    op.create_table(
        "resource_type_deletion_request",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "requested_by_identity_id", sa.String(length=64), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_rtdr_name"),
    )
    op.create_index(
        "ix_rtdr_scheduled_for",
        "resource_type_deletion_request",
        ["scheduled_for"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rtdr_scheduled_for", table_name="resource_type_deletion_request"
    )
    op.drop_table("resource_type_deletion_request")
    op.drop_index("ix_resource_instance_type", table_name="resource_instance")
    op.drop_index(
        "ix_resource_instance_tenant_type", table_name="resource_instance"
    )
    op.drop_table("resource_instance")
    op.drop_index("ix_resource_type_owner", table_name="resource_type")
    op.drop_table("resource_type")
