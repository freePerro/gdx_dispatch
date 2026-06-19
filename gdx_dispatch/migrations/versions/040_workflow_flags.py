"""Per-tenant Job workflow flags

UX audit F-8 / 2026-04-29. Default behavior on `Start Job` is to stamp
started_at + auto-assign the current user. Everything else (schedule
lock, arrival timeline event, arrival SMS, complete-time required fields)
is opt-in per tenant — tracked here.

Revision ID: 040_workflow_flags
Revises: 039_job_numbering
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "040_workflow_flags"
down_revision = "039_job_numbering"
branch_labels = None
depends_on = None


_FLAGS = (
    "workflow_lock_schedule_on_start",
    "workflow_post_arrival_event",
    "workflow_sms_arrival_notify",
    "workflow_require_parts_on_complete",
    "workflow_require_hours_on_complete",
    "workflow_require_signature_on_complete",
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
