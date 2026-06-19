"""Sprint 1.x — phone_com per-tenant credential storage on tenant_settings.

Adds 5 columns to ``tenant_settings`` for the Phone.com Voice + SMS
integration:

- phone_com_token_enc                  (Fernet-encrypted permanent or OAuth token)
- phone_com_token_set_at
- phone_com_token_last_validated_at
- phone_com_token_last_error
- phone_com_webhook_secret             (Fernet-encrypted per-tenant HMAC secret)

The RLS policy on ``tenant_settings`` (added in 033 with the canonical
``policy_sql`` pattern) attaches to the table, not per-column. Adding
columns inherits the policy automatically — no DROP/CREATE dance needed.

Revision ID: 035_phone_com_tenant_settings
Down revision: 034_audit_logs_orm_alignment
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "035_phone_com_tenant_settings"
down_revision = "034_audit_logs_orm_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_settings", sa.Column("phone_com_token_enc", sa.Text(), nullable=True))
    op.add_column("tenant_settings", sa.Column("phone_com_token_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenant_settings",
        sa.Column("phone_com_token_last_validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("tenant_settings", sa.Column("phone_com_token_last_error", sa.Text(), nullable=True))
    op.add_column("tenant_settings", sa.Column("phone_com_webhook_secret", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_settings", "phone_com_webhook_secret")
    op.drop_column("tenant_settings", "phone_com_token_last_error")
    op.drop_column("tenant_settings", "phone_com_token_last_validated_at")
    op.drop_column("tenant_settings", "phone_com_token_set_at")
    op.drop_column("tenant_settings", "phone_com_token_enc")
