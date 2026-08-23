"""Provenance for a stored part price, and for who authored an invoice line.

Two follow-ups from `closeout-parts-autopricing-plan.md`. They need the same
kind of thing: a record of WHERE a number came from, kept next to the number.

**1. ``job_parts_needed.price_source``** (follow-up 1, the plan's largest
remaining gap). Four lanes write ``unit_price`` — the office's own figure for
this job, bench inventory, the tenant/CHI catalog's sell price, and the margin
engine marking a cost up — and all four land in the same ``NUMERIC(10,2)``.
"Who priced this part and why" could not be answered from the records at all,
which is invariant #1 on money code. ``core/part_pricing.py`` already knows the
answer at the moment it resolves; it simply had nowhere to put it.

**2. ``invoice_lines.source``** (follow-up 3). ``release_untouched_autodraft``
deletes EVERY line on an untouched autodraft so the closeout can rebuild it —
including lines the office added by hand, which is exactly what the unbilled-
parts banner tells them to do. Telling machine lines from human ones is the
marker that makes the rebuild selective.

**Both are NULL for every existing row, and NULL means "unknown", not
"machine".** No backfill is attempted, deliberately: what was not recorded at
capture time cannot now be reconstructed, and a guessed provenance on money
data is worse than an honest blank. The autodraft guard therefore reads NULL as
*possibly human* and refuses to delete it.

Measured on the live tenant 2026-08-23 before writing this: 812 live invoice
lines, 5 of them on an autodraft invoice, and **zero** autodraft invoices still
in draft — so the conservative reading costs nothing that exists today. And
73 ``job_parts_needed`` rows, only 5 of them priced.

Revision ID: 075_price_line_provenance
Revises: 074_bank_feed_statement_link
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "075_price_line_provenance"
down_revision = "074_bank_feed_statement_link"
branch_labels = None
depends_on = None

# (table, column, type). Short free-text tags, not enums: a CHECK constraint
# here would need a migration every time a pricing lane is added, and the
# writers are the contract. Kept narrow so a typo cannot become a long string.
_COLUMNS = (
    ("job_parts_needed", "price_source", "VARCHAR(24)"),
    ("invoice_lines", "source", "VARCHAR(16)"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite has no ADD COLUMN IF NOT EXISTS. A fresh database already has
        # the column from the ORM metadata, so "duplicate column" is expected
        # here and is not a reason to fail the migration.
        for table, column, coltype in _COLUMNS:
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {coltype};"
                )
        return
    for table, column, coltype in _COLUMNS:
        bind.exec_driver_sql(
            f"ALTER TABLE IF EXISTS {table} "
            f"ADD COLUMN IF NOT EXISTS {column} {coltype};"
        )


def downgrade() -> None:
    # Dropping these loses provenance that cannot be recomputed — the lane a
    # price came from is only knowable at the moment it is resolved. It does
    # not lose money: both columns are descriptive, every amount survives, and
    # the autodraft guard falls back to its pre-075 behaviour of treating an
    # untouched draft as machine-owned.
    bind = op.get_bind()
    for table, column, _coltype in _COLUMNS:
        if bind.dialect.name != "postgresql":
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column};")
        else:
            bind.exec_driver_sql(
                f"ALTER TABLE IF EXISTS {table} DROP COLUMN IF EXISTS {column};"
            )
