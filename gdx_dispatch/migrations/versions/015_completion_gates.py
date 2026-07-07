"""Completion gates — invoice gate flag + no-parts attestation

PR5-billing-capture (2026-07-07).

1. ``tenant_settings.workflow_require_invoice_on_complete`` — optional hard
   gate (default OFF): a job can't complete without a billing-real invoice.
   tenant_settings IS in baseline_squashed.sql, so a plain guarded ALTER.
2. ``job_closeouts.no_parts_used`` — the tech's explicit "no parts used"
   attestation. With require_parts_on_complete ON (Doug's decision for GDX),
   the gate accepts a parts list OR this checkbox — a tech is never stuck,
   but silence still 422s. job_closeouts is ORM-created (#41 ordering) —
   to_regclass-guarded like 003.

Revision ID: 015_completion_gates
Revises: 014_parts_capture_unification
"""
from alembic import op

revision = "015_completion_gates"
down_revision = "014_parts_capture_unification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.tenant_settings') IS NOT NULL THEN
            ALTER TABLE tenant_settings
              ADD COLUMN IF NOT EXISTS workflow_require_invoice_on_complete
                BOOLEAN NOT NULL DEFAULT false;
          END IF;
          IF to_regclass('public.job_closeouts') IS NOT NULL THEN
            ALTER TABLE job_closeouts
              ADD COLUMN IF NOT EXISTS no_parts_used BOOLEAN NOT NULL DEFAULT false;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.tenant_settings') IS NOT NULL THEN
            ALTER TABLE tenant_settings
              DROP COLUMN IF EXISTS workflow_require_invoice_on_complete;
          END IF;
          IF to_regclass('public.job_closeouts') IS NOT NULL THEN
            ALTER TABLE job_closeouts DROP COLUMN IF EXISTS no_parts_used;
          END IF;
        END $$;
        """
    )
