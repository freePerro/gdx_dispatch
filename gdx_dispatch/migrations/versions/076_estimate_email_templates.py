"""Per-tenant estimate-email subject/body templates rendered when sending an
estimate via the in-app composer.

Revision ID: 076_estimate_email_templates
Revises: 075_phone_com_webhook_rotation
Create Date: 2026-05-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "076_estimate_email_templates"
down_revision = "075_phone_com_webhook_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("estimate_email_subject_template", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenant_settings",
        sa.Column("estimate_email_body_template", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "estimate_email_body_template")
    op.drop_column("tenant_settings", "estimate_email_subject_template")
