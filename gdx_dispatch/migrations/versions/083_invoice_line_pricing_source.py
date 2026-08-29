"""Record HOW an invoice line was priced, next to the number.

`estimate_lines` already answers this: 298 of 336 live rows carry a full
snapshot — `cost_snapshot`, `margin_pct_snapshot` and a `pricing_source` naming
the lane (tier 150, labor_matrix 101, line_override 26, client_cost 19,
wholesale_tier 2). You can reconstruct what was quoted and why.

`invoice_lines` cannot. Measured on the live tenant 2026-08-29 before writing
this: 832 lines, `unit_price` on all 832, `cost_snapshot` on 63, and
**`margin_pct_snapshot` on ZERO**. Every one of those 63 has `unit_price > 0`,
so all 63 were exactly derivable and none were derived. And only 9 of 365
invoices were created from an estimate — the other 356 are composed directly,
where nothing records the pricing decision at all.

Two columns, both nullable, no backfill here:

**`pricing_source VARCHAR(32)`** — the lane. Deliberately NOT the existing
`invoice_lines.source`, which is *authorship* ("autodraft" = the closeout
builder wrote this line) and is read by `core/closeout_billing.is_untouched_
autodraft` to decide whether a draft may be rebuilt. Authorship and pricing
lane are different axes; overloading one column would make "manual" ambiguous
between "a human typed this line" and "a human chose this price". 32 chars to
match `EstimateLine.pricing_source`, so a lane can be forwarded verbatim at
estimate→invoice conversion instead of being dropped.

**`pricing_inputs JSON`** — the operands behind a computed price, for lanes
where the price is a formula. The closeout service lane stores
`estimated_man_hours` from attested hours but bills first-hour + hourly × billed
hours, so a live row can read 1.50 hours against a $300 price with no way to
see the two techs that reconcile them. JSON because `job_closeouts.parts_used`
already uses it and it is portable.

Free-text tags, not a CHECK: the reasoning in 075 holds — a CHECK needs a
migration every time a lane is added, and the writers are the contract
(`core/pricing_provenance.PRICING_SOURCES`).

**NULL means "not recorded", never "unknown lane".** No backfill is attempted
here: what was not captured at the time cannot be reconstructed now, and a
guessed provenance on money data is worse than an honest blank. The 63 rows
whose margin IS arithmetically derivable are a separate, separately-approved
data migration — a derived value is a different claim from a recorded one and
must be tagged as such.

Revision ID: 083_invoice_line_pricing_source
Revises: 082_outlook_message_flag
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "083_invoice_line_pricing_source"
down_revision = "082_outlook_message_flag"
branch_labels = None
depends_on = None

# (table, column, type). No percent literals anywhere in this file, so nothing
# needs doubling for Alembic's formatter.
_COLUMNS = (
    ("invoice_lines", "pricing_source", "VARCHAR(32)"),
    ("invoice_lines", "pricing_inputs", "JSON"),
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
    # Descriptive only — dropping these loses the record of how a price was
    # reached, which cannot be recomputed after the fact, but loses no money:
    # every amount lives in unit_price/line_total and is untouched. The
    # autodraft rebuild reads `source`, not `pricing_source`, so it is
    # unaffected either way.
    bind = op.get_bind()
    for table, column, _coltype in _COLUMNS:
        if bind.dialect.name != "postgresql":
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column};")
        else:
            bind.exec_driver_sql(
                f"ALTER TABLE IF EXISTS {table} DROP COLUMN IF EXISTS {column};"
            )
