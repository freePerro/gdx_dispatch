"""Why a payment was voided, recorded next to the fact that it was.

Money-audit **M15**. Closing the dispute lifecycle means putting a payment back
when a dispute is won — and that is only safe if you can tell WHICH void the
win reverses.

`voided_at` was the entire state, and **three** different things set it:

* `core/payments.py::_reverse_recorded_payment` — a dispute or a failure,
* the same function reached from `_apply_charge_refund` — a **full Stripe
  refund**, which writes no `InvoiceAdjustment` at all,
* `routers/invoices.py::void_payment` — the office deliberately reversing one.

An adversarial review proved all three: with only `voided_at` to go on, a
`charge.dispute.funds_reinstated` un-voided a payment that a refund had
reversed, and one the office had reversed on purpose. Each reads as **money
invented** — the invoice goes back to paid on cash that is not there.

A first attempt guarded on "does this invoice carry a refund adjustment", which
cannot fire on the path that matters: the Stripe full-refund branch is a bare
`return _reverse_recorded_payment(...)` and deliberately leaves the money entry
to the office endpoint. The guard was checking for a row that path never
writes.

So the reason gets recorded. `_reverse_recorded_payment` already takes one; it
simply had nowhere to put it.

**NULL means "voided before this column existed", and the reinstate path treats
that as unknown and refuses.** No backfill: what was not recorded cannot be
reconstructed, and guessing here re-creates the money-invention this exists to
prevent.

Measured on prod 2026-08-24: **3 voided payments**, all of which will carry
NULL. That is the correct outcome — a dispute reinstatement against any of
them would refuse and ask for a human, which is exactly right for rows whose
void reason nobody recorded. None of the three is card-processor money with a
live dispute, so the refusal is theoretical for them.

Revision ID: 076_payment_voided_reason
Revises: 075_price_line_provenance
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "076_payment_voided_reason"
down_revision = "075_price_line_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite has no ADD COLUMN IF NOT EXISTS. A fresh database already has
        # the column from the ORM metadata, so "duplicate column" is expected
        # here and is not a reason to fail the migration.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "ALTER TABLE payments ADD COLUMN voided_reason VARCHAR(64);"
            )
        return
    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS payments ADD COLUMN IF NOT EXISTS voided_reason VARCHAR(64);"
    )


def downgrade() -> None:
    # Dropping this loses the provenance of every void and re-opens the
    # money-invention it prevents: the reinstate path falls back to refusing
    # every reinstatement, which is the safe direction but a functional
    # regression. It loses no money — the column is descriptive, and
    # `voided_at` still says a payment was voided.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with contextlib.suppress(Exception):
            bind.exec_driver_sql("ALTER TABLE payments DROP COLUMN voided_reason;")
        return
    bind.exec_driver_sql(
        "ALTER TABLE IF EXISTS payments DROP COLUMN IF EXISTS voided_reason;"
    )
