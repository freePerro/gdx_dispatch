"""create oauth_dcr_clients table for RFC 7591 Dynamic Client Registration"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "060_oauth_dcr_clients"
down_revision = "059_cc_mfa_granter_trigger"
branch = None
depends_on = None


def upgrade() -> None:
    # NOTE: this migration creates a new table on schema public. On
    # hardened deploys (lab/prod) the runtime app role (gdx_app) does
    # NOT hold CREATE on public — alembic must be invoked with
    # ALEMBIC_DATABASE_URL pointing at a privileged migration role
    # (the DB owner, e.g. gdx). See gdx_dispatch/migrations/env.py and the
    # sprint-mcp-streamable-http retro doc.
    op.create_table(
        "oauth_dcr_clients",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("client_secret_hash", sa.String(255), nullable=True),
        sa.Column("secret_prefix", sa.String(16), nullable=True),
        sa.Column("client_name", sa.String(255), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("grant_types", sa.JSON(), nullable=False),
        sa.Column("response_types", sa.JSON(), nullable=False),
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(64),
            nullable=False,
            server_default="client_secret_basic",
        ),
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("client_id_issued_at", sa.Integer(), nullable=False),
        sa.Column(
            "client_secret_expires_at",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("oauth_dcr_clients")
