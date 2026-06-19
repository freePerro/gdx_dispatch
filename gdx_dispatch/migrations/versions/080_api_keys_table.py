"""Create api_keys table on control plane.

The APIKey ORM model in gdx_dispatch/core/api_keys.py has shipped for months but the
table was never migrated to prod — discovered 2026-05-13 when the new public
landing-leads route (commit a2cdf004) needed to mint a tenant key via
`python -m gdx_dispatch.tools.create_api_key` and Postgres reported
`relation "api_keys" does not exist`.

Schema matches APIKey model exactly (id/tenant_id/key_hash/key_prefix/name/
scopes JSON/timestamps/expires/revoked) plus the two indexes already declared
on the model.

The model uses TenantBase as the declarative parent for historical reasons
(tenant_id column scoping) but the table is shared control-plane: every
APIKey lookup happens through SessionLocal (see APIKeyMiddleware.dispatch
and public_router._require_api_key).

Revision ID: 080_api_keys_table
Revises: 079_closeout_defaults_true
Create Date: 2026-05-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "080_api_keys_table"
down_revision = "079_closeout_defaults_true"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(as_uuid=True, native_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True, native_uuid=False), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
