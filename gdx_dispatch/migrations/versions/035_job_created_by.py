"""add created_by to jobs

Mobile job-create fix (2026-07-22): a job created from the tech-mobile
dialog carries no tech assignment (dispatch assigns later), so nothing
tied it back to its creator and /api/mobile/jobs could never show it —
the "I created a job and it didn't save" complaint. created_by stamps the
creating user's id at POST /api/jobs time. Nullable: NULL simply means
"pre-feature row"; nothing reads it as required, and visibility rules
treat it as an additive OR-clause only while the job is unassigned.

Revision ID: 035_job_created_by
Revises: 034_vendor_bill_sweep
"""
from alembic import op

revision = "035_job_created_by"
down_revision = "034_vendor_bill_sweep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # jobs is in the squashed baseline, so it always exists here; IF NOT
    # EXISTS keeps the ALTER idempotent across multi-container boots.
    op.get_bind().exec_driver_sql(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_by varchar(36) NULL"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE jobs DROP COLUMN IF EXISTS created_by"
    )
