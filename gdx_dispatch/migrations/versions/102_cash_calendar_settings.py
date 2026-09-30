"""Cash calendar settings: a cash floor and the operating accounts.

* tenant_forecast_settings.cash_floor — NUMERIC(14,2) NULL. The balance the
  operating accounts should stay above; the cash calendar names the first day
  they would not. NULL = no floor chosen yet.
* tenant_forecast_settings.operating_account_ids — JSON NULL. Bank-feed
  account ids whose balances make up operating cash. NULL = not chosen yet.

The table is created from the ORM (it has no create migration), so on a
database that does not have it yet this does nothing and create_all builds it
with both columns. Existing rows get NULL for both, which the calendar reads
as "not chosen" — nothing is backfilled and no money rows are touched.

Plan: docs/design/cash-calendar-plan.md.

Revision ID: 102_cash_calendar_settings
Revises: 101_estimate_lead_link
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "102_cash_calendar_settings"
down_revision = "101_estimate_lead_link"
branch_labels = None
depends_on = None

TABLE = "tenant_forecast_settings"


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    missing = [
        col for col in (
            sa.Column("cash_floor", sa.Numeric(14, 2), nullable=True),
            sa.Column("operating_account_ids", sa.JSON(), nullable=True),
        )
        if not _has_column(bind, TABLE, col.name)
    ]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch:
        for col in missing:
            batch.add_column(col)


def downgrade() -> None:
    """Drop both columns. They hold only the two calendar choices, which the
    user can make again; no ledger or money data lives in them."""
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    present = [c for c in ("operating_account_ids", "cash_floor") if _has_column(bind, TABLE, c)]
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in present:
            batch.drop_column(name)
