"""Create cc_backup_runs table for backup-run tracking (cc2-s54)."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "065_cc_backup_runs"
down_revision = "064_cc_support_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cc_backup_runs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),  # 'control_plane' | 'tenant_db'
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'started'")),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('control_plane', 'tenant_db')",
            name="ck_cc_backup_runs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'success', 'failed')",
            name="ck_cc_backup_runs_status",
        ),
    )
    op.create_index(
        "ix_cc_backup_runs_started",
        "cc_backup_runs",
        ["started_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_cc_backup_runs_tenant",
        "cc_backup_runs",
        ["tenant_id"],
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_cc_backup_runs_tenant", table_name="cc_backup_runs")
    op.drop_index("ix_cc_backup_runs_started", table_name="cc_backup_runs")
    op.drop_table("cc_backup_runs")
