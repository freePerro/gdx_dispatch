"""Create cc_async_jobs table for tenant provision + cohort migration (cc2-s42, s45)."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "066_cc_async_jobs"
down_revision = "065_cc_backup_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cc_async_jobs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("cohort_filter_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "params_json",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result_json",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "progress_pct",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cc_staff_users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('provision', 'cohort_migration')",
            name="ck_cc_async_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'success', 'failed', 'cancelled')",
            name="ck_cc_async_jobs_status",
        ),
        sa.CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_cc_async_jobs_progress_pct",
        ),
    )
    op.create_index(
        "ix_cc_async_jobs_kind_status",
        "cc_async_jobs",
        ["kind", "status", "created_at"],
    )
    op.create_index(
        "ix_cc_async_jobs_tenant",
        "cc_async_jobs",
        ["tenant_id"],
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_cc_async_jobs_tenant", table_name="cc_async_jobs")
    op.drop_index("ix_cc_async_jobs_kind_status", table_name="cc_async_jobs")
    op.drop_table("cc_async_jobs")
