"""Per-tenant estimate deposit percentage shown on the customer-facing PDF.

Revision ID: 077_estimate_deposit_pct
Revises: 076_estimate_email_templates
Create Date: 2026-05-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "077_estimate_deposit_pct"
down_revision = "076_estimate_email_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column(
            "estimate_deposit_pct",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "estimate_deposit_pct")
