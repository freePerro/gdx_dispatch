"""scheduled_sms — texts queued to send later.

The plan: docs/design/scheduled-sms-plan.md. Phone.com v4 cannot schedule a
message, so a reply, an invoice link or an estimate link chosen for "send
later" waits here until the ``phone_com.send_due_scheduled_sms`` beat task
sends it.

A new table only: no existing row is touched and no money column changes.
Guarded with ``has_table`` because the table is ORM-created: where
``create_all`` has already built it this does nothing.

Rollback: ``downgrade()`` drops the table. Texts still waiting are lost with
it (nothing was sent for them); texts already sent keep their
``phone_com_messages`` rows and audit rows, which live elsewhere.

Revision ID: 103_scheduled_sms
Revises: 102_cash_calendar_settings
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "103_scheduled_sms"
down_revision = "102_cash_calendar_settings"
branch_labels = None
depends_on = None

TABLE = "scheduled_sms"


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True, index=True),
        sa.Column("to_number", sa.String(40), nullable=False, index=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "customer_id", sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True,
        ),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("audit_action", sa.String(60), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("canceled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("phone_com_message_row_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
