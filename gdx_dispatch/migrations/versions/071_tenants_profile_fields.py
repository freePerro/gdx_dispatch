"""Add tenant profile fields collected at signup interstitial.

Captures address (street/city/state/postal/country), phone, employee_count,
and industry on the tenants row so we can track company size, location,
and (later) industry segmentation. All fields nullable for backwards
compatibility with the existing 13 prod tenants — backfill is manual.

Industry is shipped here as a column even though the signup UI does NOT
collect it yet (Doug 2026-05-03: "have industry as an option for later but
in place if we want to use it"). Default value is left NULL until UI lands.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "071_tenants_profile_fields"
down_revision = "070_tenants_last_active_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("street", sa.String(length=255), nullable=True))
    op.add_column("tenants", sa.Column("city", sa.String(length=120), nullable=True))
    op.add_column("tenants", sa.Column("state", sa.String(length=80), nullable=True))
    op.add_column("tenants", sa.Column("postal_code", sa.String(length=20), nullable=True))
    op.add_column("tenants", sa.Column("country", sa.String(length=2), nullable=True, server_default="US"))
    op.add_column("tenants", sa.Column("phone", sa.String(length=32), nullable=True))
    op.add_column("tenants", sa.Column("employee_count", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("industry", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "industry")
    op.drop_column("tenants", "employee_count")
    op.drop_column("tenants", "phone")
    op.drop_column("tenants", "country")
    op.drop_column("tenants", "postal_code")
    op.drop_column("tenants", "state")
    op.drop_column("tenants", "city")
    op.drop_column("tenants", "street")
