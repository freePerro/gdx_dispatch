"""Closeout→billing reconciliation toggle — plan §12.

Doug 2026-07-30: the discrepancy flow (a job re-closed-out after it was already
billed) must be switchable company-wide. This is the on/off flag; default OFF so
nothing changes for tenants who don't opt in. Lives on tenant_settings next to
the workflow_* toggles and is served through the same /api/workflow/flags
surface.

tenant_settings IS in the baseline (002 pattern) — a plain ALTER is safe.

Revision ID: 047_closeout_billing_reconcile
Revises: 046_estimate_expiry_days
"""
from alembic import op

revision = "047_closeout_billing_reconcile"
down_revision = "046_estimate_expiry_days"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "ADD COLUMN IF NOT EXISTS closeout_billing_reconciliation "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "DROP COLUMN IF EXISTS closeout_billing_reconciliation"
    )
