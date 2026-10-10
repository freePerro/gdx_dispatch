"""An optimistic-concurrency token on customers, invoices and estimates (GDXA-447).

* ``customers.version``, ``invoices.version``, ``estimates.version`` —
  ``Integer NOT NULL DEFAULT 1``. The ORM bumps it by one on every UPDATE it
  compiles for the table (a SQL-expression ``onupdate``, so an ORM flush and a
  Core/ORM bulk ``update()`` both bump it). A PATCH that carries the version its
  edit dialog loaded can then be refused with 409 when the row has moved on,
  instead of silently reverting a colleague's edit. The PATCH checks themselves
  ship with each table's owner, not here.

Existing rows: every existing customer, invoice and estimate reads
``version = 1``, filled by the column's server default as it is added. No other
column is read or written.

Portability: plain ``op.add_column`` with a constant server default, which
both engines take without a table rebuild (SQLite ``ALTER TABLE ADD COLUMN``
accepts ``NOT NULL`` with a constant default; Postgres 11+ records it in the
catalog). Guarded per table: a no-op when the table is absent or already has
the column. ``create_all`` runs before alembic on boot but never adds a column
to an existing table, so this is what reaches an existing database.

Rollback: ``downgrade()`` drops the three columns (``batch_alter_table`` so
SQLite rebuilds the table). Nothing else depends on them in the schema; code
that reads ``version`` must be rolled back first.

Revision ID: 114_row_version_tokens
Revises: 113_reseller_quotes
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "114_row_version_tokens"
down_revision = "113_reseller_quotes"
branch_labels = None
depends_on = None

_TABLES = ("customers", "invoices", "estimates")
_COLUMN = "version"


def _columns(bind, table: str) -> set[str] | None:
    """The table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        cols = _columns(bind, table)
        if cols is None or _COLUMN in cols:
            continue
        op.add_column(
            table,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=sa.text("1")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        cols = _columns(bind, table)
        if cols is None or _COLUMN not in cols:
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(_COLUMN)
