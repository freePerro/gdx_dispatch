"""SS-20 developer portal tables (renamed to developer_portal_* per 0.9-a.1).

Revision ID: ss20_dev_portal
Revises: 010_ss19_mcp_execute
Create Date: 2026-04-19

Chained 2026-04-20 by Sprint 0.9-b.
Per 0.9-a.1 (commit da159d6f), SS-20 tables were renamed from
``developer_*`` → ``developer_portal_*`` to avoid collision with the
SS-3d ``developer_accounts`` table already on canonical Base.

Creates:
    - developer_portal_accounts
    - developer_portal_email_verifications
    - developer_portal_apps
    - developer_portal_app_secrets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# NOTE: placeholder identifiers — see INTEGRATION_TODO above
revision = "ss20_dev_portal"
down_revision = "ss19_mcp_execute"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "developer_portal_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "tier",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'sandbox'"),
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tos_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_developer_accounts_email"),
    )
    op.create_index(
        "ix_developer_accounts_email", "developer_portal_accounts", ["email"], unique=True
    )

    op.create_table(
        "developer_portal_email_verifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("developer_portal_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token", name="uq_developer_email_verif_token"),
    )
    op.create_index(
        "ix_developer_email_verif_token",
        "developer_portal_email_verifications",
        ["token"],
        unique=True,
    )

    op.create_table(
        "developer_portal_apps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("developer_portal_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", name="uq_developer_apps_client_id"),
    )
    op.create_index(
        "ix_developer_apps_client_id", "developer_portal_apps", ["client_id"], unique=True
    )

    op.create_table(
        "developer_portal_app_secrets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "app_id",
            sa.Integer(),
            sa.ForeignKey("developer_portal_apps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("secret_prefix", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("app_id", "secret_prefix", name="uq_app_secret_prefix"),
    )


def downgrade() -> None:
    op.drop_table("developer_portal_app_secrets")
    op.drop_index("ix_developer_apps_client_id", table_name="developer_portal_apps")
    op.drop_table("developer_portal_apps")
    op.drop_index(
        "ix_developer_email_verif_token", table_name="developer_portal_email_verifications"
    )
    op.drop_table("developer_portal_email_verifications")
    op.drop_index("ix_developer_accounts_email", table_name="developer_portal_accounts")
    op.drop_table("developer_portal_accounts")
