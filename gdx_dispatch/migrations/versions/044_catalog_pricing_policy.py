"""Per-tenant catalog zero-pricing policy (UX audit F-75)

Four independent toggles (a/b/c/d from the audit options). Default: only
the soft warn-on-invoice is on; the rest are opt-in so existing tenants
don't get blocked by their own legacy data.

Revision ID: 044_catalog_pricing_policy
Revises: 043_catalog_policy
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "044_catalog_pricing_policy"
down_revision = "043_catalog_policy"
branch_labels = None
depends_on = None


_FLAGS = (
    ("catalog_block_zero_price_on_invoice", "false"),
    ("catalog_warn_zero_price_on_invoice", "true"),
    ("catalog_block_zero_price_on_save", "false"),
    ("catalog_auto_inactivate_zero_price", "false"),
)


def upgrade() -> None:
    for col, default in _FLAGS:
        op.add_column(
            "tenant_settings",
            sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.text(default)),
        )


def downgrade() -> None:
    for col, _ in reversed(_FLAGS):
        op.drop_column("tenant_settings", col)
