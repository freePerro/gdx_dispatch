"""Drop three columns nothing writes: invoices.total_amount, invoices.amount_paid,
jobs.dispatched_at.

Each was a trap that kept re-firing, which is why they go rather than staying
"harmless".

**invoices.total_amount** — NULL on all 349 prod rows; no insert path has ever
written it. Four revenue surfaces summed it and reported $0 against $829,164.66
of real billed work (money-audit M8). `_summary_window` was fixed alone on
2026-04-27 and its siblings were missed, so the same bug shipped twice. The
audit's own words: "a column nothing writes and five things read is a trap that
keeps re-firing."

**invoices.amount_paid** — the only writer in the repo is the one-off
`tools/qb_payment_substance_repair.py`, run on prod once (2026-07-31 10:23:58
UTC, 287 rows). By 2026-08-22 it had drifted on 24 live invoices by $62,473.72,
and seven read paths trusted it (money-audit M35).

**jobs.dispatched_at** — defined once, never written, never read, NULL on all
275 prod rows. Found while confirming that the "On My Way" button really does
record time tracking: it does, on `job_assignments.en_route_at` (21 rows) plus
the audit event. This column was never part of that path.

WHAT HAPPENS TO EXISTING ROWS
-----------------------------
`total_amount` and `dispatched_at` are NULL on every prod row (349 live
invoices, 275 jobs, checked 2026-08-22), so nothing is lost. `amount_paid`
holds 286 non-zero values across live invoices; they are a **stale cache**, and
the truth they approximate lives in `payments`. Every reader was migrated to
`core/invoice_paid.py` (Σ payments WHERE voided_at IS NULL) before this ran.

ONE ROW IS NOT DERIVABLE, and an adversarial review was right to insist it be
named. `INV-2026-0002` — soft-deleted, draft — carries `amount_paid = 2900.98`
while its only payment row was VOIDED on 2026-07-31, so Σ(live payments) is 0.
Every other divergence understates; this one overstates. The number is not
information being destroyed: the payment row itself survives, amount and
`voided_at` intact, so "a $2,900.98 payment was taken and later voided" is
still fully reconstructable from `payments`. What the column holds is a stale
echo that was never updated when the void happened — the drift itself. It is
called out here rather than swept into "nothing is lost", which the first draft
of this docstring wrongly claimed.

ROLLBACK
--------
`downgrade()` re-adds all three as nullable, then **rebuilds `amount_paid` from
the payments table** rather than restoring the stale numbers — a downgrade
should leave the column better than it was, not resurrect the drift. The other
two come back NULL, which is exactly their prior state.

Rollback is therefore NOT byte-identical: for the 24 drifted rows and for
INV-2026-0002 it produces the CORRECT paid-to-date rather than the stale one.
That is deliberate, and it is the one thing a downgrade here cannot promise.
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "073_drop_dead_money_columns"
down_revision = "072_invoice_source_estimate"
branch_labels = None
depends_on = None


_DROPS = (
    ("invoices", "total_amount"),
    ("invoices", "amount_paid"),
    ("jobs", "dispatched_at"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite pre-3.35 cannot DROP COLUMN, and the bundled version varies by
        # image. Try, and let an unread nullable column survive if it cannot —
        # failing a migration over a column nothing reads would be worse.
        for table, column in _DROPS:
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column};")
        return

    for table, column in _DROPS:
        bind.exec_driver_sql(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column};"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for stmt in (
            "ALTER TABLE invoices ADD COLUMN total_amount NUMERIC(12, 2);",
            "ALTER TABLE invoices ADD COLUMN amount_paid NUMERIC(12, 2) DEFAULT 0;",
            "ALTER TABLE jobs ADD COLUMN dispatched_at TIMESTAMP;",
        ):
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(stmt)
    else:
        bind.exec_driver_sql(
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS total_amount numeric(12,2);"
        )
        bind.exec_driver_sql(
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS amount_paid numeric(12,2) DEFAULT 0;"
        )
        bind.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dispatched_at timestamp with time zone;"
        )

    # Rebuild the cache from the source of truth rather than restoring the
    # drift. A correlated subquery, not UPDATE..FROM, so BOTH lanes run the
    # same statement — the first draft of this rolled back to all-zeros on
    # SQLite because only the Postgres branch had the rebuild, which is a
    # rollback that silently destroys a money column.
    # `total_amount` and `dispatched_at` come back NULL, precisely the state
    # they were dropped in.
    bind.exec_driver_sql(
        """
        UPDATE invoices
           SET amount_paid = COALESCE((
                   SELECT SUM(p.amount)
                     FROM payments p
                    WHERE p.invoice_id = invoices.id
                      AND p.voided_at IS NULL
               ), 0);
        """
    )
