"""Add tenants.last_active_at for the SEE Fleet "Last active" column.

The fleet grid was sourcing "Last active" from the MRR ledger, falling back
to tenants.created_at. That's a billing signal, not an activity signal —
trialing tenants always showed signup date. This adds a real per-tenant
heartbeat that TenantMiddleware bumps on each authenticated request
(throttled to ~once per minute per tenant).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "070_tenants_last_active_at"
down_revision = "069_pgcrypto_explicit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "last_active_at")
