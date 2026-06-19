"""Per-tenant maps provider selector (UX audit F-89)

Revision ID: 046_maps_provider
Revises: 045_payroll_source
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "046_maps_provider"
down_revision = "045_payroll_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("maps_provider", sa.String(length=40), nullable=False, server_default="google_maps"),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "maps_provider")
