"""add not-billable mark to jobs

RFB dismiss verb (2026-08-04): the Ready-for-Billing queue had no way to
say "this job will never be invoiced" (warranty/goodwill/internal) — the
only exits were Create Invoice or letting it sit forever. Mirrors the
wont_bill status PR4 gave leaked parts: the row keeps its audit trail but
leaves every billing nag surface. Nullable: NULL = normal billable job;
nothing reads the columns as required.

Revision ID: 055_job_not_billable
Revises: 054_remint_enumerable_pay_tokens
"""
from alembic import op

revision = "055_job_not_billable"
down_revision = "054_remint_enumerable_pay_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # jobs is in the squashed baseline, so it always exists here; IF NOT
    # EXISTS keeps the ALTERs idempotent across multi-container boots.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS not_billable_at timestamptz NULL"
    )
    bind.exec_driver_sql(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS not_billable_reason varchar(300) NULL"
    )
    bind.exec_driver_sql(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS not_billable_by varchar(36) NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE jobs DROP COLUMN IF EXISTS not_billable_by")
    bind.exec_driver_sql("ALTER TABLE jobs DROP COLUMN IF EXISTS not_billable_reason")
    bind.exec_driver_sql("ALTER TABLE jobs DROP COLUMN IF EXISTS not_billable_at")
