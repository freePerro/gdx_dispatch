"""Invoice-line quantity takes two decimals (docs/design/multi-day-jobs-plan.md §5.4b, PR A).

``invoice_lines.quantity`` goes from ``Integer`` to ``Numeric(10, 2)`` so a
labor line can say what it bills: 2.5 hours at the hourly rate, not 1 × $250
with the arithmetic hidden in the description. Only the invoice line changes.
Estimate, change-order, proposal, parts and vendor lines keep a whole-number
quantity, because they count things.

Existing rows: every value stays the same number (3 reads back as 3.00). No
total, tax or balance moves, because those are stored columns and nothing here
recomputes them. ``NOT NULL`` is kept. There is no server default; the model's
Python-side ``default=1`` is unchanged.

Portability: SQLite has no ``ALTER COLUMN``, so this uses
``batch_alter_table``, which rebuilds the table there and copies the rows.
Postgres takes ``ALTER COLUMN ... TYPE NUMERIC(10, 2)`` directly; integer to
numeric is an implicit cast, so no ``USING`` clause is needed.

Guarded like 093: a no-op when the table is absent or the column is already
``Numeric``. ``create_all`` runs before alembic on boot, so a fresh database
already has the new type.

Rollback: ``downgrade()`` is the one lossy direction, so it refuses rather
than truncates. If any line holds a fractional quantity it raises and changes
nothing; otherwise it alters the column back to ``Integer``.

Revision ID: 108_invoice_line_qty_decimal
Revises: 107_audit_logs_guard
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "108_invoice_line_qty_decimal"
down_revision = "107_audit_logs_guard"
branch_labels = None
depends_on = None

_TABLE = "invoice_lines"
_COLUMN = "quantity"


def _column_type(bind) -> sa.types.TypeEngine | None:
    """The reflected column type, or None when the table or column is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    for col in insp.get_columns(_TABLE):
        if col["name"] == _COLUMN:
            return col["type"]
    return None


def upgrade() -> None:
    bind = op.get_bind()
    current = _column_type(bind)
    if current is None or isinstance(current, sa.Numeric):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            _COLUMN,
            existing_type=sa.Integer(),
            type_=sa.Numeric(10, 2),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    current = _column_type(bind)
    if current is None or not isinstance(current, sa.Numeric):
        return
    fractional = bind.exec_driver_sql(
        f"SELECT COUNT(*) FROM {_TABLE} "  # noqa: S608 — names are module constants
        f"WHERE {_COLUMN} <> ROUND({_COLUMN}, 0)"
    ).scalar()
    if fractional:
        raise RuntimeError(
            f"refusing to downgrade 108: {fractional} invoice line(s) carry a fractional "
            "quantity, which an Integer column would truncate and so change what the "
            "line says it bills. Keep this schema by pinning the current APP_VERSION, "
            "or correct those lines first."
        )
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            _COLUMN,
            existing_type=sa.Numeric(10, 2),
            type_=sa.Integer(),
            existing_nullable=False,
        )
