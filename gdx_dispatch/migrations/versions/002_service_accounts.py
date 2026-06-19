"""Add service_accounts table (control DB) for platform-internal cross-tenant auth.

Revision ID: 002_service_accounts
Revises: 001_baseline
Create Date: 2026-04-13

Service accounts are distinct from tenant api_keys:
- Live in control DB (not per-tenant)
- Platform-owned, never visible to tenants
- Can be scoped to specific tenant slugs or all tenants (null = all)
- Authenticated via X-Service-Key header
- Actions audit-logged with actor_type=service_account

Minted by CLI (gdx_dispatch/tools/service_account_mint.py) since no platform
superadmin UI exists yet. Minting requires direct control DB access.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "002_service_accounts"
down_revision = "001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_accounts",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("allowed_tenant_slugs", sa.JSON, nullable=True),
        sa.Column("allowed_scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_service_accounts_key_hash"),
        sa.UniqueConstraint("name", name="uq_service_accounts_name"),
    )
    op.create_index("ix_service_accounts_key_prefix", "service_accounts", ["key_prefix"])


def downgrade():
    op.drop_index("ix_service_accounts_key_prefix", "service_accounts")
    op.drop_table("service_accounts")
