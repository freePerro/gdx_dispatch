"""Per-tenant Dispatch settings (scheduled-no-tech gates + lane visibility)

Doug 2026-05-01: when a job is scheduled without a tech assigned, dispatchers
need a way to either be warned (soft gate), blocked (hard gate), or have
those jobs surfaced in their own lane on the Dispatch board. All three are
opt-in per tenant.

Revision ID: 062_dispatch_settings
Revises: 061_cc_rls_silent_null
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "062_dispatch_settings"
down_revision = "061_cc_rls_silent_null"
branch_labels = None
depends_on = None


_FLAGS = (
    "dispatch_warn_save_no_tech",
    "dispatch_block_save_no_tech",
    "dispatch_show_unassigned_lane",
)


def upgrade() -> None:
    for col in _FLAGS:
        op.add_column(
            "tenant_settings",
            sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    for col in reversed(_FLAGS):
        op.drop_column("tenant_settings", col)
