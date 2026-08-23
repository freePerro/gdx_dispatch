#!/usr/bin/env python3
"""Payment-substance repair for QB-import damage — Phase 3 of the QB
paid-status repair plan (docs/design/qb-import-paid-status-repair-plan.md).

The QB importer flattened multi-invoice QB Payments onto their FIRST linked
invoice at the full TotalAmt (~$70K of phantom allocation), skipped Payment
objects whose invoice wasn't mapped at pull time (paid invoices with zero
payment rows), and never wrote ``invoices.amount_paid``. This tool repairs
the payment ROWS from QB's own per-invoice allocation lines:

1. ``split``  — every unvoided payment row larger than its invoice's total is
   reset to QB's actual allocation for that invoice; QB's allocations to the
   payment's OTHER invoices become new rows on those invoices.
2. ``backfill`` — QB-mapped invoices with no payment rows but real QB
   Payment allocations get those rows, with the real QB TxnDate. Invoices QB
   settled without a Payment (credit memo, point-of-sale) are REPORTED, not
   faked — a synthesized "payment" would be a lie; they need adjustments.
3. ``amount_paid`` — REMOVED 2026-08-22. The column was dropped (migration
   073) after every reader moved to ``core/invoice_paid.py``
   (Σ unvoided payments). This tool now repairs payment ROWS only.
4. ``--void-invoice NUMBER`` (repeatable, explicit) — void a test-junk
   invoice: status → void, its payments voided, its stale QB map deleted.

Deliberately NOT done here: no ``_recalculate_invoice`` (it rebuilds totals
from local lines, and imported invoices dropped SubTotal/Discount/Shipping
lines — a recalc silently changes their totals), no ``status``/``balance_due``
writes, no write-offs (penny residues get credit adjustments via the UI).

Usage (inside the app container — needs DB + QB env)::

    python tools/qb_payment_substance_repair.py                  # dry-run
    python tools/qb_payment_substance_repair.py --apply --operator doug \
        [--void-invoice INV-2026-0001]

Idempotent: every row this tool writes carries ``reference = 'qb:<qb payment
id>'``; re-runs see those rows and plan nothing for them. Apply is ONE
transaction with a hash-chained audit row per repaired payment/invoice.
QB access is a single read-only ``Payment`` query (a few pages).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: E402
from gdx_dispatch.core.database import SessionLocal  # noqa: E402
from gdx_dispatch.core.tenant import company_id  # noqa: E402

CENT = Decimal("0.01")


def _money(v) -> Decimal:
    return Decimal(str(v)).quantize(CENT)


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------

@dataclass
class ResetAmount:
    """An over-allocated row shrinks to QB's allocation for its own invoice."""

    payment_id: str
    invoice_number: str
    old_amount: Decimal
    new_amount: Decimal
    qb_payment_id: str
    reference: str


@dataclass
class InsertAllocation:
    """A QB allocation with no local row — sibling of a split, or backfill."""

    invoice_id: str
    invoice_number: str
    amount: Decimal
    payment_date: str  # ISO date from QB TxnDate
    qb_payment_id: str
    reference: str
    origin: str  # "split-sibling" | "backfill"


@dataclass
class SubstancePlan:
    resets: list[ResetAmount] = field(default_factory=list)
    inserts: list[InsertAllocation] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery (reads only)
# ---------------------------------------------------------------------------

def fetch_over_allocated(db) -> list[dict]:
    rows = db.execute(text(
        "SELECT p.id::text AS payment_id, i.id::text AS invoice_id, "
        "       i.invoice_number, p.amount, i.total, "
        "       pm.qb_id AS qb_payment_id, im.qb_id AS invoice_qb_id "
        "FROM payments p "
        "JOIN invoices i ON i.id = p.invoice_id "
        "LEFT JOIN qb_entity_maps pm ON pm.entity_type = 'payment' "
        "  AND pm.local_id = p.id::text "
        "LEFT JOIN qb_entity_maps im ON im.entity_type = 'invoice' "
        "  AND im.local_id = i.id::text "
        "WHERE p.voided_at IS NULL AND p.amount > i.total + 0.01 "
        "ORDER BY p.amount - i.total DESC"
    )).mappings().all()
    return [dict(r) for r in rows]


def fetch_missing_payment_invoices(db) -> list[dict]:
    """QB-mapped invoices whose money state implies payments QB never gave us:
    paid with zero rows, or partially paid (balance < total) with zero rows."""
    rows = db.execute(text(
        "SELECT i.id::text AS invoice_id, i.invoice_number, "
        "       im.qb_id AS invoice_qb_id, i.status, i.total, i.balance_due "
        "FROM invoices i "
        "JOIN qb_entity_maps im ON im.entity_type = 'invoice' "
        "  AND im.local_id = i.id::text "
        "WHERE i.deleted_at IS NULL AND i.total > 0 "
        "  AND (i.status = 'paid' OR i.balance_due < i.total - 0.01) "
        "  AND NOT EXISTS (SELECT 1 FROM payments p "
        "                  WHERE p.invoice_id = i.id AND p.voided_at IS NULL) "
        "ORDER BY i.invoice_number"
    )).mappings().all()
    return [dict(r) for r in rows]


def fetch_invoice_index(db) -> dict[str, dict]:
    """QB invoice id -> local invoice, for resolving allocation targets."""
    rows = db.execute(text(
        "SELECT im.qb_id, i.id::text AS invoice_id, i.invoice_number "
        "FROM invoices i "
        "JOIN qb_entity_maps im ON im.entity_type = 'invoice' "
        "  AND im.local_id = i.id::text "
        "WHERE i.deleted_at IS NULL"
    )).mappings().all()
    return {str(r["qb_id"]): dict(r) for r in rows}


def fetch_existing_allocation_refs(db) -> set[tuple[str, str]]:
    rows = db.execute(text(
        "SELECT invoice_id::text AS invoice_id, reference FROM payments "
        "WHERE voided_at IS NULL AND reference LIKE 'qb:%'"
    )).all()
    return {(r[0], r[1]) for r in rows}


async def fetch_qb_payment_index(db) -> dict[str, dict]:
    """One read-only pull of every QB Payment → per-invoice allocations.

    Shape: {qb_payment_id: {"date": iso, "total": Decimal,
                            "allocs": {qb_invoice_id: Decimal}}}
    Line.Amount is the allocation; only Lines whose LinkedTxn is an Invoice
    count (credit-memo/deposit lines are not invoice cash).
    """
    from gdx_dispatch.modules.quickbooks.oauth import get_qb_client  # noqa: PLC0415

    qb = await get_qb_client(company_id(), db)
    async with qb:
        payments = await qb.query("Payment")

    index: dict[str, dict] = {}
    for p in payments:
        allocs: dict[str, Decimal] = {}
        for line in p.get("Line") or []:
            amount = line.get("Amount")
            if amount is None:
                continue
            for txn in line.get("LinkedTxn") or []:
                if str(txn.get("TxnType") or "").lower() == "invoice" and txn.get("TxnId"):
                    qb_inv = str(txn["TxnId"])
                    allocs[qb_inv] = allocs.get(qb_inv, Decimal("0")) + _money(amount)
                    break
        index[str(p["Id"])] = {
            "date": str(p.get("TxnDate") or ""),
            "total": _money(p.get("TotalAmt") or 0),
            "allocs": allocs,
        }
    return index


# ---------------------------------------------------------------------------
# Planning — PURE: all lookups injected, testable without DB/QB
# ---------------------------------------------------------------------------

def build_substance_plan(
    over_allocated: list[dict],
    missing: list[dict],
    qb_index: dict[str, dict],
    invoice_index: dict[str, dict],
    existing_refs: set[tuple[str, str]],
) -> SubstancePlan:
    plan = SubstancePlan()
    planned_refs: set[tuple[str, str]] = set()

    def _has(invoice_id: str, ref: str) -> bool:
        return (invoice_id, ref) in existing_refs or (invoice_id, ref) in planned_refs

    # -- 1. splits ----------------------------------------------------------
    for row in over_allocated:
        qb_pid = str(row.get("qb_payment_id") or "")
        label = f"payment on {row['invoice_number']} (${_money(row['amount'])})"
        if not qb_pid or qb_pid not in qb_index:
            plan.issues.append(f"{label}: no QB payment mapped — cannot derive allocations")
            continue
        qb = qb_index[qb_pid]
        own_qb_inv = str(row.get("invoice_qb_id") or "")
        own_alloc = qb["allocs"].get(own_qb_inv)
        if own_alloc is None:
            plan.issues.append(
                f"{label}: QB payment {qb_pid} has no allocation for this invoice — "
                "linkage is wrong, needs eyes")
            continue
        ref = f"qb:{qb_pid}"
        plan.resets.append(ResetAmount(
            payment_id=row["payment_id"],
            invoice_number=row["invoice_number"],
            old_amount=_money(row["amount"]),
            new_amount=own_alloc,
            qb_payment_id=qb_pid,
            reference=ref,
        ))
        planned_refs.add((row["invoice_id"], ref))
        for qb_inv, amount in sorted(qb["allocs"].items()):
            if qb_inv == own_qb_inv:
                continue
            target = invoice_index.get(qb_inv)
            if not target:
                plan.issues.append(
                    f"{label}: QB allocates ${amount} to QB invoice {qb_inv}, "
                    "which is not in GDX — allocation unrepresentable")
                continue
            if _has(target["invoice_id"], ref):
                continue
            plan.inserts.append(InsertAllocation(
                invoice_id=target["invoice_id"],
                invoice_number=target["invoice_number"],
                amount=amount,
                payment_date=qb["date"],
                qb_payment_id=qb_pid,
                reference=ref,
                origin="split-sibling",
            ))
            planned_refs.add((target["invoice_id"], ref))

    # -- 2. backfills -------------------------------------------------------
    # Reverse index once: qb invoice id -> [(qb_payment_id, amount, date)]
    by_invoice: dict[str, list[tuple[str, Decimal, str]]] = {}
    for qb_pid, qb in qb_index.items():
        for qb_inv, amount in qb["allocs"].items():
            by_invoice.setdefault(qb_inv, []).append((qb_pid, amount, qb["date"]))

    for inv in missing:
        allocs = by_invoice.get(str(inv["invoice_qb_id"]), [])
        if not allocs:
            plan.issues.append(
                f"invoice {inv['invoice_number']} ({inv['status']}, "
                f"${_money(inv['total'])}): QB has NO Payment for it — settled by "
                "credit memo / point-of-sale; needs an adjustment, not a payment row")
            continue
        for qb_pid, amount, date in sorted(allocs):
            ref = f"qb:{qb_pid}"
            if _has(inv["invoice_id"], ref):
                continue
            plan.inserts.append(InsertAllocation(
                invoice_id=inv["invoice_id"],
                invoice_number=inv["invoice_number"],
                amount=amount,
                payment_date=date,
                qb_payment_id=qb_pid,
                reference=ref,
                origin="backfill",
            ))
            planned_refs.add((inv["invoice_id"], ref))

    return plan


# ---------------------------------------------------------------------------
# Apply (one transaction, audit row per repaired payment/invoice)
# ---------------------------------------------------------------------------

def apply_plan(db, plan: SubstancePlan, operator: str, void_numbers: list[str]) -> dict:
    tenant = company_id()
    actor = f"cli:{operator}"

    for r in plan.resets:
        db.execute(text(
            "UPDATE payments SET amount = :amt, reference = :ref "
            "WHERE id = CAST(:pid AS uuid) AND voided_at IS NULL"
        ), {"amt": str(r.new_amount), "ref": r.reference, "pid": r.payment_id})
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="qb_substance_repair_split_reset",
            entity_type="payment", entity_id=r.payment_id,
            details={"invoice_number": r.invoice_number,
                     "old_amount": str(r.old_amount), "new_amount": str(r.new_amount),
                     "qb_payment_id": r.qb_payment_id},
        )

    for ins in plan.inserts:
        pid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO payments (id, invoice_id, amount, method, payment_date, "
            "                      reference, created_at, company_id) "
            "VALUES (CAST(:id AS uuid), CAST(:inv AS uuid), :amt, 'quickbooks', "
            "        CAST(:date AS date), :ref, now(), :cid)"
        ), {"id": pid, "inv": ins.invoice_id, "amt": str(ins.amount),
            "date": ins.payment_date, "ref": ins.reference, "cid": tenant})
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action=f"qb_substance_repair_{ins.origin.replace('-', '_')}",
            entity_type="payment", entity_id=pid,
            details={"invoice_number": ins.invoice_number, "amount": str(ins.amount),
                     "payment_date": ins.payment_date,
                     "qb_payment_id": ins.qb_payment_id},
        )

    voided = []
    for number in void_numbers:
        row = db.execute(text(
            "SELECT id::text FROM invoices WHERE invoice_number = :n"
        ), {"n": number}).first()
        if not row:
            raise SystemExit(f"--void-invoice {number}: not found — aborting (nothing committed)")
        inv_id = row[0]
        db.execute(text(
            "UPDATE payments SET voided_at = now() "
            "WHERE invoice_id = CAST(:iid AS uuid) AND voided_at IS NULL"
        ), {"iid": inv_id})
        db.execute(text(
            "UPDATE invoices SET status = 'void' WHERE id = CAST(:iid AS uuid)"
        ), {"iid": inv_id})
        db.execute(text(
            "DELETE FROM qb_entity_maps WHERE entity_type = 'invoice' AND local_id = :iid"
        ), {"iid": inv_id})
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="qb_substance_repair_void_test_invoice",
            entity_type="invoice", entity_id=inv_id,
            details={"invoice_number": number},
        )
        voided.append(number)

    # The amount_paid backfill step lived here and ran once, on prod,
    # 2026-07-31 10:23:58 UTC (287 rows). It is GONE because the column is
    # gone (migration 073): every reader now derives paid-to-date from the
    # payments table via core/invoice_paid.py, which is what this step was
    # approximating. Re-running this tool repairs payment ROWS only — which
    # was always the substantive half.
    db.commit()
    return {"voided": voided}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_plan(db, plan: SubstancePlan) -> None:
    total_shed = sum((r.old_amount - r.new_amount for r in plan.resets), Decimal("0"))
    print(f"\n== split: {len(plan.resets)} over-allocated row(s), "
          f"${total_shed} of phantom allocation removed ==")
    for r in plan.resets:
        print(f"  {r.invoice_number:<16} ${r.old_amount} → ${r.new_amount}  "
              f"[QB payment {r.qb_payment_id}]")
    siblings = [i for i in plan.inserts if i.origin == "split-sibling"]
    backfills = [i for i in plan.inserts if i.origin == "backfill"]
    print(f"\n== new allocation rows: {len(siblings)} split sibling(s), "
          f"{len(backfills)} backfill(s) ==")
    for i in plan.inserts:
        print(f"  {i.invoice_number:<16} +${i.amount}  {i.payment_date}  "
              f"[{i.origin}, QB payment {i.qb_payment_id}]")
    if plan.issues:
        print(f"\n  ⚠ {len(plan.issues)} item(s) not repairable from QB data:")
        for issue in plan.issues:
            print(f"    - {issue}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the plan (default: dry-run report)")
    ap.add_argument("--operator", default="",
                    help="who is running this (required with --apply; audited)")
    ap.add_argument("--void-invoice", action="append", default=[], metavar="NUMBER",
                    help="void this test-junk invoice (+ its payments and QB map); repeatable")
    args = ap.parse_args()

    if args.apply and not args.operator.strip():
        ap.error("--apply requires --operator")

    db = SessionLocal()
    try:
        over = fetch_over_allocated(db)
        missing = fetch_missing_payment_invoices(db)
        invoice_index = fetch_invoice_index(db)
        existing_refs = fetch_existing_allocation_refs(db)
        qb_index = asyncio.run(fetch_qb_payment_index(db))
        print(f"QB payments pulled: {len(qb_index)}; over-allocated rows: {len(over)}; "
              f"invoices missing payment rows: {len(missing)}")

        plan = build_substance_plan(over, missing, qb_index, invoice_index, existing_refs)
        _print_plan(db, plan)

        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply --operator <you>.")
            return 0

        result = apply_plan(db, plan, operator=args.operator.strip(),
                            void_numbers=args.void_invoice)
        print(f"\nApplied: {len(plan.resets)} reset(s), {len(plan.inserts)} new row(s)"
              + (f", voided {result['voided']}" if result["voided"] else "")
              + ". Audit rows written (qb_substance_repair_*).")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
