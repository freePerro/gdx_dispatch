"""Per-tenant default terms text rendered on estimate PDFs.

Revision ID: 048_estimates_default_terms
Revises: 047_estimates_features
Create Date: 2026-04-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "048_estimates_default_terms"
down_revision = "047_estimates_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("estimates_default_terms", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "estimates_default_terms")
