"""Planning rules for tools/qb_payment_substance_repair.py (QB repair Phase 3).

Same shape as the Phase 1 planner tests: the DB/QB I/O runs only against
prod (dry-run first), but the decision rules — what gets reset, what gets
inserted, what is refused — are pure and pinned here on plain dicts.

All fixture names/ids/amounts are fictional.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.qb_payment_substance_repair import build_substance_plan  # noqa: E402


def _qb_payment(date="2025-06-01", total="1000.00", allocs=None):
    return {
        "date": date,
        "total": Decimal(total),
        "allocs": {k: Decimal(v) for k, v in (allocs or {}).items()},
    }


INVOICE_INDEX = {
    "qb-inv-1": {"invoice_id": "inv-1", "invoice_number": "N-0001"},
    "qb-inv-2": {"invoice_id": "inv-2", "invoice_number": "N-0002"},
    "qb-inv-3": {"invoice_id": "inv-3", "invoice_number": "N-0003"},
}


def _over(payment_id="pay-1", invoice_id="inv-1", number="N-0001",
          amount="1000.00", total="200.00", qb_payment_id="qb-pay-9",
          invoice_qb_id="qb-inv-1"):
    return {
        "payment_id": payment_id, "invoice_id": invoice_id,
        "invoice_number": number, "amount": Decimal(amount),
        "total": Decimal(total), "qb_payment_id": qb_payment_id,
        "invoice_qb_id": invoice_qb_id,
    }


def test_split_resets_own_amount_and_inserts_siblings():
    qb_index = {"qb-pay-9": _qb_payment(allocs={
        "qb-inv-1": "200.00", "qb-inv-2": "300.00", "qb-inv-3": "500.00"})}
    plan = build_substance_plan([_over()], [], qb_index, INVOICE_INDEX, set())

    assert not plan.issues
    assert len(plan.resets) == 1
    assert plan.resets[0].new_amount == Decimal("200.00")
    assert plan.resets[0].reference == "qb:qb-pay-9"
    assert {(i.invoice_id, str(i.amount)) for i in plan.inserts} == {
        ("inv-2", "300.00"), ("inv-3", "500.00")}
    assert all(i.origin == "split-sibling" for i in plan.inserts)
    assert all(i.payment_date == "2025-06-01" for i in plan.inserts)


def test_split_without_qb_map_or_own_allocation_is_refused_untouched():
    no_map = build_substance_plan([_over(qb_payment_id="")], [], {}, INVOICE_INDEX, set())
    assert not no_map.resets and not no_map.inserts
    assert "no QB payment mapped" in no_map.issues[0]

    wrong_link = build_substance_plan(
        [_over()], [],
        {"qb-pay-9": _qb_payment(allocs={"qb-inv-2": "1000.00"})},  # nothing for qb-inv-1
        INVOICE_INDEX, set())
    assert not wrong_link.resets and not wrong_link.inserts
    assert "no allocation for this invoice" in wrong_link.issues[0]


def test_sibling_allocation_to_unknown_invoice_is_reported_not_dropped_silently():
    qb_index = {"qb-pay-9": _qb_payment(allocs={
        "qb-inv-1": "200.00", "qb-inv-ghost": "800.00"})}
    plan = build_substance_plan([_over()], [], qb_index, INVOICE_INDEX, set())

    assert len(plan.resets) == 1  # own reset still proceeds
    assert not plan.inserts
    assert "not in GDX" in plan.issues[0]


def test_existing_reference_rows_suppress_reinserts_idempotently():
    qb_index = {"qb-pay-9": _qb_payment(allocs={
        "qb-inv-1": "200.00", "qb-inv-2": "300.00"})}
    plan = build_substance_plan(
        [_over()], [], qb_index, INVOICE_INDEX,
        existing_refs={("inv-2", "qb:qb-pay-9")})
    assert len(plan.resets) == 1
    assert not plan.inserts  # sibling already materialized on a prior run


def test_backfill_creates_rows_with_real_qb_dates():
    qb_index = {
        "qb-pay-1": _qb_payment(date="2025-03-10", allocs={"qb-inv-2": "150.00"}),
        "qb-pay-2": _qb_payment(date="2025-04-01", allocs={"qb-inv-2": "50.00"}),
    }
    missing = [{"invoice_id": "inv-2", "invoice_number": "N-0002",
                "invoice_qb_id": "qb-inv-2", "status": "paid",
                "total": Decimal("200.00"), "balance_due": Decimal("0.00")}]
    plan = build_substance_plan([], missing, qb_index, INVOICE_INDEX, set())

    assert not plan.issues
    assert {(str(i.amount), i.payment_date) for i in plan.inserts} == {
        ("150.00", "2025-03-10"), ("50.00", "2025-04-01")}
    assert all(i.origin == "backfill" for i in plan.inserts)


def test_credit_settled_invoice_is_reported_never_faked():
    missing = [{"invoice_id": "inv-3", "invoice_number": "N-0003",
                "invoice_qb_id": "qb-inv-3", "status": "paid",
                "total": Decimal("500.00"), "balance_due": Decimal("0.00")}]
    plan = build_substance_plan([], missing, {}, INVOICE_INDEX, set())

    assert not plan.inserts
    assert "needs an adjustment, not a payment row" in plan.issues[0]


def test_split_sibling_and_backfill_of_same_invoice_do_not_double_insert():
    # inv-2 is both a sibling of pay-1's split AND in the missing list; the
    # allocation may only materialize once.
    qb_index = {"qb-pay-9": _qb_payment(allocs={
        "qb-inv-1": "200.00", "qb-inv-2": "300.00"})}
    missing = [{"invoice_id": "inv-2", "invoice_number": "N-0002",
                "invoice_qb_id": "qb-inv-2", "status": "paid",
                "total": Decimal("300.00"), "balance_due": Decimal("0.00")}]
    plan = build_substance_plan([_over()], missing, qb_index, INVOICE_INDEX, set())

    inv2_rows = [i for i in plan.inserts if i.invoice_id == "inv-2"]
    assert len(inv2_rows) == 1
    assert inv2_rows[0].origin == "split-sibling"


def test_multiple_lines_to_same_invoice_accumulate_in_index_shape():
    # fetch_qb_payment_index sums Line amounts per invoice; the planner just
    # trusts the summed alloc — pin that a single summed alloc yields one row.
    qb_index = {"qb-pay-9": _qb_payment(allocs={"qb-inv-1": "200.00",
                                                "qb-inv-2": "999.99"})}
    plan = build_substance_plan([_over(total="200.00")], [], qb_index,
                                INVOICE_INDEX, set())
    assert plan.resets[0].new_amount == Decimal("200.00")
    assert len([i for i in plan.inserts if i.invoice_id == "inv-2"]) == 1
