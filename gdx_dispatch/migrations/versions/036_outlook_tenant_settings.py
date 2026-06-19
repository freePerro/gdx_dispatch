"""Add outlook_* columns to tenant_settings (control plane).

Revision ID: 036_outlook_tenant_settings
Revises: 035_phone_com_tenant_settings
Create Date: 2026-04-27

Sprint Outlook Integration — slice S3.
Per-tenant Microsoft Entra ID app credentials. Doug registers an app per
tenant in Azure portal (slice S0 runbook); the values get pasted into
Settings → Integrations → Outlook (slice S39).

All 4 columns nullable — existing tenants haven't connected Outlook yet.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "036_outlook_tenant_settings"
down_revision = "035_phone_com_tenant_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("outlook_microsoft_tenant_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tenant_settings",
        sa.Column("outlook_client_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tenant_settings",
        sa.Column("outlook_client_secret_enc", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenant_settings",
        sa.Column("outlook_secret_set_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "outlook_secret_set_at")
    op.drop_column("tenant_settings", "outlook_client_secret_enc")
    op.drop_column("tenant_settings", "outlook_client_id")
    op.drop_column("tenant_settings", "outlook_microsoft_tenant_id")
