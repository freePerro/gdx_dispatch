"""Add seat_count to tenants for billing.

Captures the number of paid seats picked at signup (or via portal upgrade).
Drives the Stripe line_items quantity at Checkout time and at subsequent
plan changes. Defaults to 1 so the existing prod tenants don't break — the
real seat counts get reconciled from Stripe Subscription.items[0].quantity
on the next subscription.updated webhook.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "073_tenants_seat_count"
down_revision = "072_platform_login_lookup_fn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("seat_count", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("tenants", "seat_count")
