"""Add phone_com_webhook_callback_id + phone_com_webhook_listener_id

Sprint phone-com fix-it Wave E / S9. Persists Phone.com's returned IDs
after webhook registration so the UI can show "Webhook: registered
(callback #N)" and an operator can look the callback up in the
Phone.com console without grepping logs.

Revision ID: 037_phone_com_webhook_ids
Revises: 036_outlook_tenant_settings
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "037_phone_com_webhook_ids"
down_revision = "036_outlook_tenant_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column(
            "phone_com_webhook_callback_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )
    op.add_column(
        "tenant_settings",
        sa.Column(
            "phone_com_webhook_listener_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "phone_com_webhook_listener_id")
    op.drop_column("tenant_settings", "phone_com_webhook_callback_id")
