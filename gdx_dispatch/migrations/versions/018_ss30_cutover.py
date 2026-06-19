"""SS-30 — cutover_schedule + deprecated_table_record.

TODO: chained on placeholder ``down_revision = "ss29_shadow_migration"``.
The supervisor will retarget this to the tip of the main chain at
end-of-sprint. Revision id uses the sprint slug so grep-find works.

Creates:
    - cutover_schedule          (per (tenant, old_table) cutover record
                                 + scheduled_drop_at consulted by the
                                 cleanup cron and extend-deprecation
                                 endpoint)
    - deprecated_table_record   (append-only ledger of every
                                 *_v1_deprecated table the cron dropped)

Column definitions mirror ``gdx_dispatch/models/platform_ss30_additions.py``
(``SS30Base`` declarative). Both tables are control-plane only — they
never touch data-plane rows.

Revision ID: ss30_cutover
Down revision: TODO
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers.
revision = "ss30_cutover"
down_revision = "ss29_shadow_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cutover_schedule",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("old_table", sa.String(length=128), nullable=False),
        sa.Column("new_table", sa.String(length=128), nullable=False),
        sa.Column("deprecated_table", sa.String(length=160), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_drop_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "extended_count",
            sa.String(length=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_identity_id", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "old_table", name="uq_cs_tenant_table"),
    )
    op.create_index(
        "ix_cs_scheduled_drop_at", "cutover_schedule", ["scheduled_drop_at"],
    )
    op.create_index("ix_cs_tenant", "cutover_schedule", ["tenant_id"])

    op.create_table(
        "deprecated_table_record",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("old_table", sa.String(length=128), nullable=True),
        sa.Column("deprecated_table", sa.String(length=160), nullable=False),
        sa.Column("scheduled_drop_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("actor_identity_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dtr_tenant", "deprecated_table_record", ["tenant_id"])
    op.create_index(
        "ix_dtr_dropped_at", "deprecated_table_record", ["dropped_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dtr_dropped_at", table_name="deprecated_table_record")
    op.drop_index("ix_dtr_tenant", table_name="deprecated_table_record")
    op.drop_table("deprecated_table_record")
    op.drop_index("ix_cs_tenant", table_name="cutover_schedule")
    op.drop_index("ix_cs_scheduled_drop_at", table_name="cutover_schedule")
    op.drop_table("cutover_schedule")
