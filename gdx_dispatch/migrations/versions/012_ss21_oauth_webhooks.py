"""SS-21 third-party OAuth + webhooks tables.

Revision ID: ss21_oauth_webhooks
Revises: TODO
Create Date: 2026-04-19

TODO:
    - set ``down_revision`` to the actual latest revision in the main chain
      once the SS-20 migration (TODO_ss20_dev_portal_XXXX.py) is re-chained,
      then chain this one after that. Proposed order once integrated:
          068 → ss20_dev_portal → ss21_oauth_webhooks
    - rename this file to the next sequential number (e.g.
      ``070_ss21_oauth_webhooks.py``) at that time.
    - remove this TODO block.

Creates:
    - ss21_authorization_codes
    - ss21_oauth_tokens
    - ss21_admin_consent_grants
    - ss21_webhook_subscriptions
    - ss21_webhook_signing_keys
    - ss21_webhook_deliveries

All tables are prefixed ``ss21_`` to make the integration merge (which may
rename them to their permanent home) unambiguous in the diff.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ss21_oauth_webhooks"
down_revision = "ss20_dev_portal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # ss21_authorization_codes
    # -----------------------------------------------------------------
    op.create_table(
        "ss21_authorization_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("code_challenge", sa.String(length=255), nullable=True),
        sa.Column("code_challenge_method", sa.String(length=16), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "admin_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("code", name="uq_ss21_authcodes_code"),
    )
    op.create_index(
        "ix_ss21_authcodes_code", "ss21_authorization_codes", ["code"], unique=True
    )
    op.create_index(
        "ix_ss21_authcodes_client", "ss21_authorization_codes", ["client_id"]
    )
    op.create_index(
        "ix_ss21_authcodes_tenant", "ss21_authorization_codes", ["tenant_id"]
    )
    op.create_index(
        "ix_ss21_authcodes_subject", "ss21_authorization_codes", ["subject_id"]
    )

    # -----------------------------------------------------------------
    # ss21_oauth_tokens
    # -----------------------------------------------------------------
    op.create_table(
        "ss21_oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("access_token", sa.String(length=128), nullable=False),
        sa.Column("refresh_token", sa.String(length=128), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("access_token", name="uq_ss21_oauth_access"),
        sa.UniqueConstraint("refresh_token", name="uq_ss21_oauth_refresh"),
    )
    op.create_index(
        "ix_ss21_oauth_access", "ss21_oauth_tokens", ["access_token"], unique=True
    )
    op.create_index(
        "ix_ss21_oauth_refresh", "ss21_oauth_tokens", ["refresh_token"], unique=True
    )
    op.create_index("ix_ss21_oauth_client", "ss21_oauth_tokens", ["client_id"])
    op.create_index("ix_ss21_oauth_tenant", "ss21_oauth_tokens", ["tenant_id"])

    # -----------------------------------------------------------------
    # ss21_admin_consent_grants
    # -----------------------------------------------------------------
    op.create_table(
        "ss21_admin_consent_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("granted_by", sa.String(length=64), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_ss21_admin_grant_pair"),
    )
    op.create_index(
        "ix_ss21_admin_grant_tenant", "ss21_admin_consent_grants", ["tenant_id"]
    )
    op.create_index(
        "ix_ss21_admin_grant_client", "ss21_admin_consent_grants", ["client_id"]
    )

    # -----------------------------------------------------------------
    # ss21_webhook_subscriptions
    # -----------------------------------------------------------------
    op.create_table(
        "ss21_webhook_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("events", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ss21_hook_sub_client", "ss21_webhook_subscriptions", ["client_id"]
    )
    op.create_index(
        "ix_ss21_hook_sub_tenant", "ss21_webhook_subscriptions", ["tenant_id"]
    )

    # -----------------------------------------------------------------
    # ss21_webhook_signing_keys
    # -----------------------------------------------------------------
    op.create_table(
        "ss21_webhook_signing_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("ss21_webhook_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kid", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ss21_hook_keys_kid", "ss21_webhook_signing_keys", ["kid"]
    )

    # -----------------------------------------------------------------
    # ss21_webhook_deliveries
    # -----------------------------------------------------------------
    op.create_table(
        "ss21_webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("ss21_webhook_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column(
            "attempt_number",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "succeeded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_ss21_hook_deliv_event", "ss21_webhook_deliveries", ["event_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ss21_hook_deliv_event", table_name="ss21_webhook_deliveries")
    op.drop_table("ss21_webhook_deliveries")
    op.drop_index("ix_ss21_hook_keys_kid", table_name="ss21_webhook_signing_keys")
    op.drop_table("ss21_webhook_signing_keys")
    op.drop_index("ix_ss21_hook_sub_tenant", table_name="ss21_webhook_subscriptions")
    op.drop_index("ix_ss21_hook_sub_client", table_name="ss21_webhook_subscriptions")
    op.drop_table("ss21_webhook_subscriptions")
    op.drop_index("ix_ss21_admin_grant_client", table_name="ss21_admin_consent_grants")
    op.drop_index("ix_ss21_admin_grant_tenant", table_name="ss21_admin_consent_grants")
    op.drop_table("ss21_admin_consent_grants")
    op.drop_index("ix_ss21_oauth_tenant", table_name="ss21_oauth_tokens")
    op.drop_index("ix_ss21_oauth_client", table_name="ss21_oauth_tokens")
    op.drop_index("ix_ss21_oauth_refresh", table_name="ss21_oauth_tokens")
    op.drop_index("ix_ss21_oauth_access", table_name="ss21_oauth_tokens")
    op.drop_table("ss21_oauth_tokens")
    op.drop_index("ix_ss21_authcodes_subject", table_name="ss21_authorization_codes")
    op.drop_index("ix_ss21_authcodes_tenant", table_name="ss21_authorization_codes")
    op.drop_index("ix_ss21_authcodes_client", table_name="ss21_authorization_codes")
    op.drop_index("ix_ss21_authcodes_code", table_name="ss21_authorization_codes")
    op.drop_table("ss21_authorization_codes")
