"""Estimate expiry window as a company setting — plan §15 win/loss.

Doug 2026-07-29: expiry window is a company setting, default 60 days. On send,
estimates get valid_until = sent_at + this many days; a nightly task then marks
past-due sent estimates 'expired'. Before this, valid_until was only ever set
if a user hand-picked a date, so the expire-stale path never fired.

tenant_settings IS in the baseline (002 pattern) — a plain ALTER is safe.
NOT NULL default 60 so every existing tenant inherits the 60-day window.

Revision ID: 046_estimate_expiry_days
Revises: 045_closeout_matrix_item
"""
from alembic import op

revision = "046_estimate_expiry_days"
down_revision = "045_closeout_matrix_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "ADD COLUMN IF NOT EXISTS estimate_expiry_days INTEGER NOT NULL DEFAULT 60"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings DROP COLUMN IF EXISTS estimate_expiry_days"
    )
