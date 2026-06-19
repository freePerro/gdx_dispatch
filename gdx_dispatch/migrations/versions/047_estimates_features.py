"""Per-tenant estimates feature toggles.

Revision ID: 047_estimates_features
Revises: 046_maps_provider
Create Date: 2026-04-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "047_estimates_features"
down_revision = "046_maps_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column(
            "estimates_allow_line_margin_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "estimates_allow_line_margin_override")
