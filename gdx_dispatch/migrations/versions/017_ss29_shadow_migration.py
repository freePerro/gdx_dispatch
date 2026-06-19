"""SS-29 — shadow_migration_state + shadow_migration_checkpoint + shadow_migration_drift.

TODO: chained on placeholder ``down_revision = "ss28_audit"``.
The supervisor will retarget this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - shadow_migration_state       (per (tenant, old_table) mode row)
    - shadow_migration_checkpoint  (backfill resume state)
    - shadow_migration_drift       (append-only drift evidence)

All tables are NEW and strictly additive — SS-29 does not touch any
existing data-plane table; every dual-write target is itself a NEW v2
table that lives alongside v1 (see ``shadow_maps.json``).

Revision ID: ss29_shadow_migration
Down revision: TODO
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers.
revision = "ss29_shadow_migration"
down_revision = "ss28_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_migration_state",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("old_table", sa.String(length=128), nullable=False),
        sa.Column("new_table", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="off"),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "old_table", name="uq_sms_tenant_table"),
    )
    op.create_index("ix_sms_tenant", "shadow_migration_state", ["tenant_id"])

    op.create_table(
        "shadow_migration_checkpoint",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("old_table", sa.String(length=128), nullable=False),
        sa.Column("last_row_id", sa.BigInteger(), nullable=True),
        sa.Column("last_row_pk", sa.String(length=128), nullable=True),
        sa.Column(
            "row_count_this_session",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "old_table", name="uq_smc_tenant_table"),
    )

    op.create_table(
        "shadow_migration_drift",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("old_table", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("old_hash", sa.String(length=64), nullable=True),
        sa.Column("new_hash", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_smd_tenant_created",
        "shadow_migration_drift",
        ["tenant_id", "created_at"],
    )
    op.create_index("ix_smd_table", "shadow_migration_drift", ["old_table"])


def downgrade() -> None:
    op.drop_index("ix_smd_table", table_name="shadow_migration_drift")
    op.drop_index("ix_smd_tenant_created", table_name="shadow_migration_drift")
    op.drop_table("shadow_migration_drift")
    op.drop_table("shadow_migration_checkpoint")
    op.drop_index("ix_sms_tenant", table_name="shadow_migration_state")
    op.drop_table("shadow_migration_state")
