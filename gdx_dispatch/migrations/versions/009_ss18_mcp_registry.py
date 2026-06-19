"""SS-18 MCP tool registry tables.

Revision ID: ss18_mcp_registry
Revises: INTEGRATION_TODO
Create Date: 2026-04-19

INTEGRATION_TODO:
    - set `down_revision` to the actual latest revision in the main
      chain at integration time (at time of writing the head is
      `068_server_defaults_mobile_fixtures`).
    - rename this file to the next sequential number (e.g.
      `069_ss18_mcp_registry.py`) when it is merged into the main
      alembic chain.
    - remove the `INTEGRATION_TODO` placeholder.

Creates:
    * ``mcp_tool_registration`` — persistent tool catalog
    * ``mcp_tool_execution_audit`` — per-call audit log

See `gdx_dispatch/models/platform_ss18_additions.py` for the column specs and
design notes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ss18_mcp_registry"
down_revision = "ss17_security_definer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_tool_registration",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("version", sa.String(length=32), nullable=False, server_default=sa.text("'1'")),
        sa.Column(
            "sensitivity_class",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        sa.Column("capabilities_required", sa.JSON(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_mcp_tool_registration_name",
        "mcp_tool_registration",
        ["name"],
        unique=True,
    )

    op.create_table(
        "mcp_tool_execution_audit",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("identity_id", sa.String(length=36), nullable=False),
        sa.Column("capabilities_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("approval_ref", sa.String(length=36), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_mcp_tool_execution_audit_tool_name",
        "mcp_tool_execution_audit",
        ["tool_name"],
    )
    op.create_index(
        "ix_mcp_tool_execution_audit_tenant_id",
        "mcp_tool_execution_audit",
        ["tenant_id"],
    )
    op.create_index(
        "ix_mcp_tool_execution_audit_identity_id",
        "mcp_tool_execution_audit",
        ["identity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_tool_execution_audit_identity_id",
        table_name="mcp_tool_execution_audit",
    )
    op.drop_index(
        "ix_mcp_tool_execution_audit_tenant_id",
        table_name="mcp_tool_execution_audit",
    )
    op.drop_index(
        "ix_mcp_tool_execution_audit_tool_name",
        table_name="mcp_tool_execution_audit",
    )
    op.drop_table("mcp_tool_execution_audit")
    op.drop_index(
        "ix_mcp_tool_registration_name",
        table_name="mcp_tool_registration",
    )
    op.drop_table("mcp_tool_registration")
