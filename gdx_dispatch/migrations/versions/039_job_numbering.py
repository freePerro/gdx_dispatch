"""Add tenant-configurable job number format + counter

UX audit F-11 / 2026-04-29. Tenants pick a format template ("JOB-{year}-{seq:03d}",
"JOB-{seq:04d}", or a custom string) and a starting sequence number. The
control plane tracks the next sequence + the last year seen so per-year
resets work without coordination from the tenant DB.

Revision ID: 039_job_numbering
Revises: 038_estimate_archive_policy
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "039_job_numbering"
down_revision = "038_estimate_archive_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_settings",
        sa.Column(
            "job_number_format",
            sa.String(length=200),
            nullable=False,
            server_default="JOB-{year}-{seq:03d}",
        ),
    )
    op.add_column(
        "tenant_settings",
        sa.Column(
            "job_number_next_seq",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "tenant_settings",
        sa.Column(
            "job_number_year_seen",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "job_number_year_seen")
    op.drop_column("tenant_settings", "job_number_next_seq")
    op.drop_column("tenant_settings", "job_number_format")
