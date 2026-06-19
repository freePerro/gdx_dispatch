"""Per-tenant payroll source selector (UX audit F-82)

Revision ID: 045_payroll_source
Revises: 044_catalog_pricing_policy
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "045_payroll_source"
down_revision = "044_catalog_pricing_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("payroll_source", sa.String(length=40), nullable=False, server_default="manual"),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "payroll_source")
