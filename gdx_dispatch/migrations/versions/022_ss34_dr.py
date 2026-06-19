"""SS-34 — dr_snapshot_manifest + dr_drill_run + dr_verification_report.

TODO: chained on placeholder ``down_revision = "ss33_resource_extensibility"``.
The supervisor will retarget this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - dr_snapshot_manifest    — one row per produced snapshot.
    - dr_drill_run            — one row per scheduled drill (pk=drill_run_id).
    - dr_verification_report  — one row per verification run (append-only).

All tables are NEW and strictly additive — SS-34 does not touch any
existing data-plane table.

Revision ID: ss34_dr_drill
Down revision: TODO
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers.
revision = "ss34_dr_drill"
down_revision = "ss33_resource_extensibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dr_snapshot_manifest",
        sa.Column("id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("scope_description", sa.String(length=256), nullable=False),
        sa.Column("backup_location", sa.Text(), nullable=False),
        sa.Column("source_db_redacted", sa.Text(), nullable=True),
    )
    op.create_index("ix_dr_snap_created", "dr_snapshot_manifest", ["created_at"])
    op.create_index("ix_dr_snap_sha", "dr_snapshot_manifest", ["sha256"])

    op.create_table(
        "dr_drill_run",
        sa.Column("drill_run_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("scope_selector", sa.String(length=256), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("scheduled_by_identity_id", sa.String(length=64), nullable=True),
        sa.Column("staging_db_redacted", sa.Text(), nullable=True),
        sa.Column("report_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_dr_drill_scheduled", "dr_drill_run", ["scheduled_for"])
    op.create_index("ix_dr_drill_status", "dr_drill_run", ["passed"])

    op.create_table(
        "dr_verification_report",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("drill_run_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checks_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_dr_verify_drill", "dr_verification_report", ["drill_run_id"])
    op.create_index("ix_dr_verify_created", "dr_verification_report", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_dr_verify_created", table_name="dr_verification_report")
    op.drop_index("ix_dr_verify_drill", table_name="dr_verification_report")
    op.drop_table("dr_verification_report")
    op.drop_index("ix_dr_drill_status", table_name="dr_drill_run")
    op.drop_index("ix_dr_drill_scheduled", table_name="dr_drill_run")
    op.drop_table("dr_drill_run")
    op.drop_index("ix_dr_snap_sha", table_name="dr_snapshot_manifest")
    op.drop_index("ix_dr_snap_created", table_name="dr_snapshot_manifest")
    op.drop_table("dr_snapshot_manifest")
