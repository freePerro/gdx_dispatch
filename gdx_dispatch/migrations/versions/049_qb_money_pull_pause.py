"""QuickBooks money-pull pause — payment-date plan, QB phase-out.

Doug 2026-07-30: QB is being phased out in favor of bank feeds; payment
corrections (including 2025 dates) are entered in GDX. While that happens,
the QB invoice/payment pulls must not run: the payment pull dedupes by QB
entity id only (double-entry mints duplicate rows) and its update branch
re-stamps payment_date from QB (reverting GDX-side corrections). The
existing kill switch only trips on the GL ledger flag, which is CPA-gated —
this column is the narrow standalone pause. Default OFF: nothing changes
until the office flips it in Settings before starting the backfill.

tenant_settings IS in the baseline (002 pattern) — a plain ALTER is safe.

Revision ID: 049_qb_money_pull_pause
Revises: 048_job_number_unique
"""
from alembic import op

revision = "049_qb_money_pull_pause"
down_revision = "048_job_number_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "ADD COLUMN IF NOT EXISTS qb_money_pull_paused "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE tenant_settings "
        "DROP COLUMN IF EXISTS qb_money_pull_paused"
    )
