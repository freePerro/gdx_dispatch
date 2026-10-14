#!/usr/bin/env python3
"""Zero the balance on void invoices that still carry one (GDXA-267).

A void owes nothing, but two writers left voids showing money owed:

* ``_recalculate_invoice``: until GDXA-267 every recalc on a void (a
  PaymentIntent that succeeded after the void, the refund that voided that
  payment again, a redelivered webhook) re-derived the balance as
  ``total - paid - credited`` while the chokepoint (#422) correctly refused to
  move the status off ``void``. Fixed in the same change as this tool.
* A void written outside ``void_invoice`` that never zeroed the balance. The
  one prod row (INV-2026-0001, $100.00, read 2026-10-06) is this case:
  ``tools/qb_payment_substance_repair.py --void-invoice`` set
  ``status = 'void'`` by raw SQL on 2026-07-31 and left ``balance_due`` alone.
  No recalc was involved in that row.

So this tool does not claim a cause per row; it records the balance it replaced.

No AR total is wrong today: every AR reader filters ``status = 'void'``. The
row is wrong to anyone who reads it raw (the customer portal lists it), and it
stays wrong forever, because nothing recalculates a void on its own.

What this writes: ``balance_due = 0`` on each void invoice whose balance is
not already zero. Nothing else: ``total``, ``subtotal`` and the payment rows
are the record of what the invoice was and what moved, and they stay. Every row
gets an audit event carrying the balance it replaced. Re-runnable: a zeroed
row is not selected twice.

Usage (inside the app container)::

    python tools/void_invoice_balance_repair.py                      # dry-run
    python tools/void_invoice_balance_repair.py --apply --operator doug

Rollback: every affected id and its previous balance are printed and audited
(``void_invoice_balance_zeroed``, ``details.previous_balance_due``). To undo
a row::

    UPDATE invoices SET balance_due = <previous_balance_due> WHERE id = '<id>';
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: E402
from gdx_dispatch.core.database import SessionLocal  # noqa: E402
from gdx_dispatch.core.tenant import company_id  # noqa: E402
from gdx_dispatch.models.tenant_models import Invoice  # noqa: E402


@dataclass
class ArmedVoid:
    invoice_id: str
    invoice_number: str | None
    total: Decimal
    balance_due: Decimal


def fetch_armed(db) -> list[ArmedVoid]:
    """Void invoices whose balance is not zero.

    ORM, not raw SQL: SQLite stores a Uuid as 32 dashless hex, and a raw
    ``id = :dashed`` comparison silently matches nothing there.
    """
    rows = db.execute(
        select(Invoice)
        .where(Invoice.status == "void", Invoice.balance_due != 0)
        .order_by(Invoice.invoice_number)
    ).scalars().all()
    return [
        ArmedVoid(
            invoice_id=str(r.id),
            invoice_number=r.invoice_number,
            total=Decimal(str(r.total or 0)),
            balance_due=Decimal(str(r.balance_due or 0)),
        )
        for r in rows
    ]


def _print_plan(armed: list[ArmedVoid]) -> None:
    if not armed:
        print("No void invoice carries a balance. Nothing to do.")
        return
    print(f"\n{len(armed)} void invoice(s) carrying a balance:\n")
    print(f"  {'invoice_id':38} {'number':14} {'total':>12} {'balance_due':>12}")
    for a in armed:
        print(f"  {a.invoice_id:38} {(a.invoice_number or '-'):14} "
              f"{a.total:>12,.2f} {a.balance_due:>12,.2f}")
    print(f"\n  Balance to zero: {sum(a.balance_due for a in armed):,.2f}")
    print("  Totals and payment rows are left as they are.")


def apply_plan(db, armed: list[ArmedVoid], *, operator: str) -> int:
    tenant = company_id()
    actor = f"cli:{operator}"
    done = 0
    for a in armed:
        inv = db.get(Invoice, UUID(a.invoice_id))
        if inv is None or inv.status != "void":
            continue
        inv.balance_due = Decimal("0.00")
        done += 1
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="void_invoice_balance_zeroed",
            entity_type="invoice", entity_id=a.invoice_id,
            details={
                "invoice_number": a.invoice_number,
                "total": float(a.total),
                # What we replaced, kept forever: the rollback value.
                "previous_balance_due": float(a.balance_due),
                "balance_due": 0.0,
                "reason": "a void owes nothing; balance_due was not zero (GDXA-267)",
            },
        )
    db.commit()
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the plan (default: dry-run report)")
    ap.add_argument("--operator", default="",
                    help="who is running this (required with --apply; audited)")
    args = ap.parse_args()

    if args.apply and not args.operator.strip():
        ap.error("--apply requires --operator")

    db = SessionLocal()
    try:
        armed = fetch_armed(db)
        _print_plan(armed)

        if not armed:
            return 0
        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply --operator <you>.")
            return 0

        n = apply_plan(db, armed, operator=args.operator.strip())
        print(f"\nApplied: {n} void invoice(s) zeroed. "
              "Audit rows written (void_invoice_balance_zeroed).")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
