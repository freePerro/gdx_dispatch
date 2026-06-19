"""Default dispatch_show_unassigned_lane to TRUE for every tenant.

Doug 2026-05-01: the "Scheduled — Not Assigned" lane is a safety net every
dispatcher benefits from. Migration 062 shipped it as opt-in (default false)
to be conservative; this flips it on by default and backfills existing rows.
The two gates (warn / block) stay opt-in because they change save behavior;
showing the lane only adds a read-only view, so it's safe-on.

Revision ID: 063_dispatch_lane_default_on
Revises: 062_dispatch_settings
Create Date: 2026-05-01
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "063_dispatch_lane_default_on"
down_revision = "062_dispatch_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New rows: true going forward.
    op.execute(
        "ALTER TABLE tenant_settings "
        "ALTER COLUMN dispatch_show_unassigned_lane SET DEFAULT true"
    )
    # Existing rows: backfill. 062 just shipped today with default false, so
    # no tenant has had time to deliberately disable it — flipping every row
    # is the same as honoring the new default.
    op.execute(text("UPDATE tenant_settings SET dispatch_show_unassigned_lane = true"))


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_settings "
        "ALTER COLUMN dispatch_show_unassigned_lane SET DEFAULT false"
    )
    op.execute(text("UPDATE tenant_settings SET dispatch_show_unassigned_lane = false"))
