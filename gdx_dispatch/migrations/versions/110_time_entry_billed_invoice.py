"""Day rows are billed through a point (docs/design/multi-day-jobs-plan.md §5.4b, PR B).

* ``time_entries.billed_invoice_id`` — ``Uuid``, nullable, FK ``invoices.id``,
  indexed. The invoice that billed this day row. A labor line claims the job's
  unbilled day rows with ``... WHERE billed_invoice_id IS NULL`` and prices
  from the rows it actually stamped, so a second invoice on the job cannot bill
  the same days again. A void, an invoice delete, or deleting the invoice's
  last attested labor line sets it back to NULL.
* Backfill: ``invoice_lines.pricing_source = 'labor_attested'`` where
  ``labor_source = 'attested'`` and ``pricing_source`` is NULL or ``'manual'``.
  Those are the older attested labor lines: the autodraft built them with a
  bare ``InvoiceLine()`` (NULL) and the create path stamped ``'manual'`` on a
  picker line. After this they count as "first hour already charged" for the
  job, which is the truth. A label only: no amount, quantity or status moves.

Existing rows: ``billed_invoice_id`` is NULL on every time entry. Day rows only
exist since migration 106, which had not reached production when this was
written, so no billed day row is left unstamped. The backfill touches only
lines matching the predicate above.

Portability: ``batch_alter_table`` (SQLite rebuilds the table, Postgres takes
plain ``ALTER TABLE``). Guarded like 106: a no-op when ``time_entries`` is
absent or already has the column; ``create_all`` runs before alembic on boot,
so a fresh database already has it. The backfill is idempotent and skipped
when ``invoice_lines`` or either column is absent.

Rollback: ``downgrade()`` drops the index and the column. The provenance
backfill stays: it is a true label, and the older code ignores it.

Revision ID: 110_time_entry_billed_invoice
Revises: 109_stripe_webhook_events
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "110_time_entry_billed_invoice"
down_revision = "109_stripe_webhook_events"
branch_labels = None
depends_on = None

_TABLE = "time_entries"
_COLUMN = "billed_invoice_id"
_INDEX = "ix_time_entries_billed_invoice_id"
_FK = "fk_time_entries_billed_invoice_id_invoices"


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

    line_cols = _columns(bind, "invoice_lines")
    if line_cols is not None and {"pricing_source", "labor_source"} <= line_cols:
        bind.exec_driver_sql(
            "UPDATE invoice_lines SET pricing_source = 'labor_attested' "
            "WHERE labor_source = 'attested' "
            "AND (pricing_source IS NULL OR pricing_source = 'manual')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, _TABLE)
    if cols is None or _COLUMN not in cols:
        return
    if _INDEX in _indexes(bind, _TABLE):
        op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
