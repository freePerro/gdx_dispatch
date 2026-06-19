"""SS-23 event bus tables.

Revision ID: ss23_event_bus
Revises: INTEGRATION_TODO
Create Date: 2026-04-19

INTEGRATION_TODO:
    - set ``down_revision`` to the actual latest revision in the main
      chain (at time of writing: "068") once SS-24 integration lands
      and SS23Base is merged onto the primary platform Base.
    - rename this file to the next sequential number (e.g.
      ``069_ss23_event_bus.py``) at that time.
    - remove the ``INTEGRATION_TODO`` placeholder and mount the router
      in ``gdx_dispatch/main.py``.

Creates:
    - event_subscription         (per-installation event-type subscription)
    - event_drain_checkpoint     (drain worker bookkeeping; 1:1 event_outbox)

Does NOT alter event_outbox — SS-10's schema is preserved. All SS-23
lifecycle bookkeeping lives in event_drain_checkpoint. If/when SS-24
wants to add the streamed_at / delivered_to_all_at / delivery_error_count
columns described in the SS-23 plan (P40), that is a follow-up migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# NOTE: placeholder identifiers — see INTEGRATION_TODO above
revision = "ss23_event_bus"
down_revision = "ss21_oauth_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_subscription",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("installation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_name", sa.String(length=128), nullable=False),
        sa.Column("webhook_url", sa.String(length=1024), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_event_subscription_install",
        "event_subscription",
        ["installation_id"],
    )
    op.create_index(
        "ix_event_subscription_event_name",
        "event_subscription",
        ["event_name"],
    )

    op.create_table(
        "event_drain_checkpoint",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("event_outbox_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_event_drain_checkpoint_event",
        "event_drain_checkpoint",
        ["event_outbox_id"],
        unique=True,
    )
    op.create_index(
        "ix_event_drain_checkpoint_status",
        "event_drain_checkpoint",
        ["status"],
    )
    op.create_index(
        "ix_event_drain_checkpoint_retry_after",
        "event_drain_checkpoint",
        ["retry_after"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_drain_checkpoint_retry_after", table_name="event_drain_checkpoint")
    op.drop_index("ix_event_drain_checkpoint_status", table_name="event_drain_checkpoint")
    op.drop_index("ix_event_drain_checkpoint_event", table_name="event_drain_checkpoint")
    op.drop_table("event_drain_checkpoint")
    op.drop_index("ix_event_subscription_event_name", table_name="event_subscription")
    op.drop_index("ix_event_subscription_install", table_name="event_subscription")
    op.drop_table("event_subscription")
