"""SS-35 — sar_request + erasure_request + pii_event_log.

TODO: chained on placeholder ``down_revision = "ss34_dr_drill"``.
The supervisor will retarget this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - sar_request       — Subject Access Request filings
    - erasure_request   — right-to-erasure filings + cooloff + approval
    - pii_event_log     — append-only hash-chained SAR/erasure log

All tables are NEW and strictly additive — SS-35 does not touch any
existing data-plane table. Registrations against those tables happen
at Python-import time via :mod:`gdx_dispatch.core.pii_fields`.

Revision ID: ss35_pii_tracking
Down revision: TODO
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers.
revision = "ss35_pii_tracking"
down_revision = "ss34_dr_drill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sar_request",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("target_identity_id", sa.String(length=64), nullable=False),
        sa.Column("requested_by_identity_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("export_json", sa.JSON(), nullable=True),
        sa.Column("download_token", sa.String(length=256), nullable=True),
        sa.Column("download_issued_at", sa.String(length=64), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sar_target", "sar_request", ["target_identity_id"])
    op.create_index("ix_sar_status", "sar_request", ["status"])

    op.create_table(
        "erasure_request",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("target_identity_id", sa.String(length=64), nullable=False),
        sa.Column("requested_by_identity_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("cooloff_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by_identity_id", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("affected_field_count", sa.BigInteger(), nullable=True),
        sa.Column("affected_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_erasure_target", "erasure_request", ["target_identity_id"])
    op.create_index("ix_erasure_status", "erasure_request", ["status"])
    op.create_index("ix_erasure_cooloff", "erasure_request", ["cooloff_until"])

    op.create_table(
        "pii_event_log",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("event_name", sa.String(length=128), nullable=False),
        sa.Column("target_identity_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pii_log_identity", "pii_event_log", ["target_identity_id"])
    op.create_index("ix_pii_log_event", "pii_event_log", ["event_name"])


def downgrade() -> None:
    op.drop_index("ix_pii_log_event", table_name="pii_event_log")
    op.drop_index("ix_pii_log_identity", table_name="pii_event_log")
    op.drop_table("pii_event_log")
    op.drop_index("ix_erasure_cooloff", table_name="erasure_request")
    op.drop_index("ix_erasure_status", table_name="erasure_request")
    op.drop_index("ix_erasure_target", table_name="erasure_request")
    op.drop_table("erasure_request")
    op.drop_index("ix_sar_status", table_name="sar_request")
    op.drop_index("ix_sar_target", table_name="sar_request")
    op.drop_table("sar_request")
