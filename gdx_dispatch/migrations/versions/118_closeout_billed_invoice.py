"""A closeout records the invoice that billed its final-day hours (GDXA-798).

* ``job_closeouts.billed_invoice_id`` — ``Uuid``, nullable, FK ``invoices.id``,
  indexed. Declared like ``time_entries.billed_invoice_id`` (migration 110).
  ``core/billing_lanes.job_labor_lines`` adds a closeout's ``hours_worked`` x
  ``techs_on_site`` every time it is handed one, and nothing marked those
  hours as billed, so a second invoice on the job, or a reopen and a
  hand-built invoice, offered them again (GDXA-725 [2]). This migration is the
  schema only; stamping, reading and releasing the column is the billing
  change that follows it.

Existing rows: ``billed_invoice_id`` is NULL on every closeout, so every
closeout written before this keeps today's behaviour. No amount, line or
status moves. Not a row-refusing migration: it never raises on data.

Portability: ``batch_alter_table`` (SQLite rebuilds the table, Postgres takes
plain ``ALTER TABLE``). The SQLite rebuild re-creates the partial unique index
``uq_job_closeouts_live`` from reflection; the migration test checks it keeps
its ``WHERE``. Guarded like 110: a no-op when ``job_closeouts`` is absent or
already has the column; ``create_all`` runs before alembic on boot, so a fresh
database already has it.

Rollback: ``downgrade()`` drops the index and the column. Any stamp written
since is lost with it, and the older code never read it.

Revision ID: 118_closeout_billed_invoice
Revises: 117_trade_estimate_tax_rate_zero
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "118_closeout_billed_invoice"
down_revision = "117_trade_estimate_tax_rate_zero"
branch_labels = None
depends_on = None

_TABLE = "job_closeouts"
_COLUMN = "billed_invoice_id"
_INDEX = "ix_job_closeouts_billed_invoice_id"
_FK = "fk_job_closeouts_billed_invoice_id_invoices"


def _columns(bind, table: str) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return {col["name"] for col in insp.get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, _TABLE)
    if cols is not None and _COLUMN not in cols:
        # The FK only when its target exists: a partial schema must not fail
        # the whole upgrade for want of a constraint.
        fk = (
            [sa.ForeignKey("invoices.id", name=_FK)]
            if _columns(bind, "invoices") is not None
            else []
        )
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column(_COLUMN, sa.Uuid(), *fk, nullable=True))
    cols = _columns(bind, _TABLE)
    if cols is not None and _COLUMN in cols and _INDEX not in _indexes(bind, _TABLE):
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, _TABLE)
    if cols is None or _COLUMN not in cols:
        return
    if _INDEX in _indexes(bind, _TABLE):
        op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
