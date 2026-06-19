"""SS-24 metering + billing tables.

Revision ID: ss24_metering
Revises: ss23_event_bus
Create Date: 2026-04-19

Chains directly off the SS-23 event-bus migration (``down_revision =
"ss23_event_bus"``) so the metering pipeline tables apply immediately after
the event-bus bookkeeping tables. At supervisor-integration time this pair
gets retargeted onto the canonical main chain (e.g. 069 → 070) and
SS24Base merges onto the primary platform Base.

Creates:
    - metering_usage          (per-period per-tenant per-event-type counter)
    - metering_checkpoint     (aggregator idempotency marker)
    - billing_plan            (tenant plan + per-event-type limits)
    - billing_overage_event   (detected overages, one-per-period per-event)

Does NOT alter event_outbox or any SS-10 / SS-23 tables.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# NOTE: placeholder identifiers — renamed to NNN_ss24_metering.py at the
# supervisor-integration slice.
revision = "ss24_metering"
down_revision = "ss23_event_bus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metering_usage",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("period_kind", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("aggregated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            name="uq_metering_usage_period_tenant_event",
        ),
    )
    op.create_index(
        "ix_metering_usage_tenant_period",
        "metering_usage",
        ["tenant_id", "period_start"],
    )

    op.create_table(
        "metering_checkpoint",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("period_kind", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("last_event_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("last_emitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quantity_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            name="uq_metering_checkpoint_period_tenant_event",
        ),
    )

    op.create_table(
        "billing_plan",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("plan_code", sa.String(length=64), nullable=False, server_default="free"),
        sa.Column("period_kind", sa.String(length=16), nullable=False, server_default="month"),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_subscription_item_id_by_event", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_billing_plan_tenant",
        "billing_plan",
        ["tenant_id"],
        unique=True,
    )

    op.create_table(
        "billing_overage_event",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("period_kind", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("limit_value", sa.BigInteger(), nullable=False),
        sa.Column("observed_quantity", sa.BigInteger(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("emitted_event_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("notes", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_billing_overage_period_tenant_event",
        "billing_overage_event",
        ["period_kind", "period_start", "tenant_id", "event_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_overage_period_tenant_event",
        table_name="billing_overage_event",
    )
    op.drop_table("billing_overage_event")
    op.drop_index("ix_billing_plan_tenant", table_name="billing_plan")
    op.drop_table("billing_plan")
    op.drop_table("metering_checkpoint")
    op.drop_index("ix_metering_usage_tenant_period", table_name="metering_usage")
    op.drop_table("metering_usage")
