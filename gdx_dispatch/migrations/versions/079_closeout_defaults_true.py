"""Default the three closeout-gate flags to TRUE for new tenants.

Phase 2 / C5 (Doug 2026-05-10). The closeout dialog (C3) submits parts +
hours + signature to /api/jobs/{id}/closeout, which checks the same
tenant gates as the legacy /complete:
    workflow_require_parts_on_complete
    workflow_require_hours_on_complete
    workflow_require_signature_on_complete

Prior default was FALSE (set in migration 040). That meant every NEW
tenant signed up with no completion gates — undermining the whole point
of Phase 2 (capture parts + hours + signature at close, not later).

This migration ALTERs the column server_default to TRUE for those three
flags. Existing rows are NOT touched — the GDX tenant (and the other
two existing tenants) keep their actual values (currently false until
Doug toggles them via /settings). Only new INSERTs that don't specify
the column take the new default.

The other three workflow flags (lock_schedule_on_start, post_arrival_event,
sms_arrival_notify) stay defaulted to false — those toggles add side
effects (SMS, timeline rows) that may not be wanted out-of-the-box.

Revision ID: 079_closeout_defaults_true
Revises: 078_cc_audit_chain_hex
Create Date: 2026-05-10

NOTE on the rename: the original draft was 079_workflow_closeout_defaults_true
(38 chars). alembic_version.version_num is varchar(32) — that name
triggered psycopg2.StringDataRightTruncation when alembic stamped the
new head. Per memory/project_cc_v2_platform_gotchas.md: keep revision
strings ≤32 chars. The current name is 26.
"""
from __future__ import annotations

from alembic import op


revision = "079_closeout_defaults_true"
down_revision = "078_cc_audit_chain_hex"
branch_labels = None
depends_on = None


_FLAGS = (
    "workflow_require_parts_on_complete",
    "workflow_require_hours_on_complete",
    "workflow_require_signature_on_complete",
)


def upgrade() -> None:
    # Idempotent — running twice is harmless.
    for col in _FLAGS:
        op.execute(f'ALTER TABLE tenant_settings ALTER COLUMN "{col}" SET DEFAULT true')


def downgrade() -> None:
    for col in _FLAGS:
        op.execute(f'ALTER TABLE tenant_settings ALTER COLUMN "{col}" SET DEFAULT false')
