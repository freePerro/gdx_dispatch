"""Add estimate_draft_archive_days to tenant_settings

2026-04-29 UX audit: per-tenant policy for how many days a Draft estimate
sits idle before it auto-archives. Default 60 days; 0 disables.

Revision ID: 038_estimate_archive_policy
Revises: 037_phone_com_webhook_ids
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "038_estimate_archive_policy"
down_revision = "037_phone_com_webhook_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column(
            "estimate_draft_archive_days",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "estimate_draft_archive_days")
