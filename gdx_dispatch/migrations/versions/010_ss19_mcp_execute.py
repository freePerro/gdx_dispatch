"""SS-19 MCP execute/SSE execution log.

Revision ID: ss19_mcp_execute
Revises: INTEGRATION_TODO
Create Date: 2026-04-19

INTEGRATION_TODO:
    - set `down_revision` to the actual latest revision in the main
      chain at integration time (at time of writing, the SS-18
      TODO migration declares its own INTEGRATION_TODO; SS-19 chains
      after SS-18).
    - rename this file to the next sequential number (e.g.
      `070_ss19_mcp_execute.py`) when it is merged into the main
      alembic chain.
    - remove the `INTEGRATION_TODO` placeholder.

Creates:
    * ``mcp_execution_log`` — per-invocation execution record (runtime
      metadata complementing SS-18 ``mcp_tool_execution_audit``)

See ``gdx_dispatch/models/platform_ss19_additions.py`` for the column specs
and design notes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ss19_mcp_execute"
down_revision = "ss18_mcp_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_execution_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("identity_id", sa.String(length=36), nullable=False),
        sa.Column("capabilities_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("input_redacted", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_mcp_execution_log_trace_id",
        "mcp_execution_log",
        ["trace_id"],
    )
    op.create_index(
        "ix_mcp_execution_log_tool_name",
        "mcp_execution_log",
        ["tool_name"],
    )
    op.create_index(
        "ix_mcp_execution_log_tenant_id",
        "mcp_execution_log",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_execution_log_tenant_id", table_name="mcp_execution_log")
    op.drop_index("ix_mcp_execution_log_tool_name", table_name="mcp_execution_log")
    op.drop_index("ix_mcp_execution_log_trace_id", table_name="mcp_execution_log")
    op.drop_table("mcp_execution_log")
