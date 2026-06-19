"""Phase 1.4 — webhook secret rotation columns on tenant_settings.

Phone.com does NOT sign webhook payloads, so URL-secret rotation is the
only hardening lever we have. The rotator generates a new secret, PATCHes
the callback URL on Phone.com to point at the new path, and copies the
old secret into ``_prev`` columns with a 1-hour grace window so any
in-flight retries Phone.com is still pushing against the old URL still
authenticate.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "075_phone_com_webhook_rotation"
down_revision = "074_tenants_terms_accepted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column("phone_com_webhook_secret_prev", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenant_settings",
        sa.Column(
            "phone_com_webhook_secret_prev_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "tenant_settings",
        sa.Column(
            "phone_com_webhook_rotated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "phone_com_webhook_rotated_at")
    op.drop_column("tenant_settings", "phone_com_webhook_secret_prev_until")
    op.drop_column("tenant_settings", "phone_com_webhook_secret_prev")
