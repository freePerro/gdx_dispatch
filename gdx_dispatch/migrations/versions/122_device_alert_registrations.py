"""device_alert_registrations — phones that get office alerts.

The Android app's foreground service polls the bell feed with an opaque token; this table holds
the token's SHA-256, its owner and when it was revoked.

A new table only: no existing row is touched and no money column changes.
Guarded with ``has_table`` because the table is ORM-created: where
``create_all`` has already built it this does nothing. Plain columns only, so
it runs the same on SQLite and Postgres.

Rollback: ``downgrade()`` drops the table. Every phone with alerts on then
gets a 401 on its next poll, turns its alerts off and says so; nothing else
is affected.

Revision ID: 122_device_alert_registrations
Revises: 121_trade_estimate_tax_rate_null
Create Date: 2026-10-04 (renumbered from 105 to 121 on 2026-10-13 and to 122 on 2026-10-14: main took 105-121 first)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "122_device_alert_registrations"
down_revision = "121_trade_estimate_tax_rate_null"
branch_labels = None
depends_on = None

TABLE = "device_alert_registrations"


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("device_label", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
