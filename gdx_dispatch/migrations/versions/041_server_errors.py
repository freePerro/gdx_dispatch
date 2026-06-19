"""Self-hosted server-side error sink

UX audit F-18 / 2026-04-29. Replaces Sentry. Captures every unhandled
exception (and 5xx HTTPException) with full context — route, tenant,
user, traceback, request fingerprint — so we can:
  - alert on new error classes without paying Sentry's per-event cost
  - feed our own AI watcher (NORTH_STAR roadmap) on schema we own
  - keep prod traces inside our own RLS-enforced control plane

Grouping is by `group_fingerprint` (route + exception class + top
traceback frame). Resolution workflow is admin-gated: resolved_at +
resolved_by + resolution_note. Soft-resolution; the row stays for
trend analysis.

Revision ID: 041_server_errors
Revises: 040_workflow_flags
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "041_server_errors"
down_revision = "040_workflow_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_errors",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("request_id", sa.String(length=64), nullable=True, index=True),
        sa.Column("method", sa.String(length=10), nullable=True),
        sa.Column("path", sa.Text(), nullable=True, index=True),
        sa.Column("status_code", sa.Integer(), nullable=True, index=True),
        sa.Column("exception_class", sa.String(length=200), nullable=True, index=True),
        sa.Column("exception_message", sa.Text(), nullable=True),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("user_email", sa.String(length=254), nullable=True),
        sa.Column("query_string", sa.Text(), nullable=True),
        sa.Column("referer", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("git_sha", sa.String(length=40), nullable=True, index=True),
        sa.Column("group_fingerprint", sa.String(length=64), nullable=True, index=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            index=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    # Composite index for the most common admin filter: open errors by tenant,
    # newest first.
    op.create_index(
        "ix_server_errors_open_recent",
        "server_errors",
        ["tenant_id", "occurred_at"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_server_errors_open_recent", table_name="server_errors")
    op.drop_table("server_errors")
