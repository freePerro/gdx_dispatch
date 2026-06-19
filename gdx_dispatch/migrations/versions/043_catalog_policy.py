"""Per-tenant catalog description policy (UX audit F-74)

Three independent toggles. Default: render-fallback ON, require + AI-suggest OFF.

Revision ID: 043_catalog_policy
Revises: 042_billing_terms
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "043_catalog_policy"
down_revision = "042_billing_terms"
branch_labels = None
depends_on = None


_FLAGS = (
    ("catalog_require_description", "false"),
    ("catalog_render_name_when_desc_empty", "true"),
    ("catalog_ai_suggest_descriptions", "false"),
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
