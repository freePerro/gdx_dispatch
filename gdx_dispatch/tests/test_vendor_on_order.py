"""On-order view — committed spend, threaded to what followed it.

The supplier's ORDER NUMBER becomes their INVOICE NUMBER (verified against real
mail: 8 of 10 sampled confirmations already appear as invoice numbers on a bill
or a statement line). That one key threads the lifecycle, and an order with
neither behind it is spend GDX could not otherwise see.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
from gdx_dispatch.modules.vendor_orders.models import VendorOrder, VendorOrderLine
from gdx_dispatch.modules.vendor_orders.onorder import (
    STATUS_AWAITING_BILL,
    STATUS_BILLED,
    STATUS_ON_STATEMENT,
    STATUS_SETTLED,
    build_on_order,
)
from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine

VENDOR = "Example Door Supply"


def _order(db, number, total, order_date=date(2026, 7, 23), code="ACME01",
           ship_to="A Jobsite", po=None, lines=()):
    o = VendorOrder(
        vendor_name=VENDOR, vendor_code=code, order_number=number,
        order_date=order_date, ship_to=ship_to, customer_po=po,
        estimated_subtotal=Decimal(str(total)), estimated_total=Decimal(str(total)),
        parser_name="midwest_order_v1", parser_version=1, line_count=len(lines),
        source="email",
    )
    db.add(o)
    db.flush()
    for i, (desc, note, qty, cost) in enumerate(lines):
        db.add(VendorOrderLine(
            order_id=o.id, line_no=i, description=desc, notes=note,
            quantity=Decimal(str(qty)), unit="EA",
            unit_cost=Decimal(str(cost)), line_total=Decimal(str(cost)) * Decimal(str(qty)),
        ))
    db.flush()
    return o


def _bill(db, invoice_number, total):
    inv = VendorInvoice(
        vendor_name_raw=VENDOR, vendor_key=VENDOR.lower(),
        invoice_number=invoice_number, total=Decimal(str(total)),
        source="email", extraction_method="parser",
    )
    db.add(inv)
    db.flush()
    return inv


def _statement_line(db, invoice_number, balance):
    st = VendorStatement(
        vendor_name=VENDOR, vendor_code="ACME01", statement_date=date(2026, 7, 25),
        parser_name="midwest_v1", parser_version=1,
        raw_total=Decimal(str(balance)), line_count=1, status="parsed",
    )
    db.add(st)
    db.flush()
    db.add(VendorStatementLine(
        statement_id=st.id, line_no=0, vendor_invoice_no=invoice_number,
        amount=Decimal(str(balance)), balance=Decimal(str(balance)),
        aging_bucket="0-29", line_date=date(2026, 7, 1),
    ))
    db.flush()


# --------------------------------------------------------------------------- #
# the point: spend that isn't billed yet
# --------------------------------------------------------------------------- #
def test_an_order_with_no_bill_is_committed_spend(tenant_db):
    _order(tenant_db, "20635854", "3707.74")
    tenant_db.commit()

    summary = build_on_order(tenant_db)[0]
    assert summary.awaiting_bill_count == 1
    assert summary.awaiting_bill_total == Decimal("3707.74")
    assert summary.items[0].status == STATUS_AWAITING_BILL
    assert summary.items[0].billed_total is None
    assert summary.items[0].variance is None


def test_an_order_threads_to_a_bill_on_the_same_number(tenant_db):
    _order(tenant_db, "20473426", "10782.39")
    _bill(tenant_db, "20473426", "10782.39")
    tenant_db.commit()

    item = build_on_order(tenant_db)[0].items[0]
    assert item.status == STATUS_BILLED
    assert item.billed_total == Decimal("10782.39")
    assert item.variance == Decimal("0.00")


def test_an_order_the_supplier_is_asking_for_reads_as_on_statement(tenant_db):
    _order(tenant_db, "20552277", "5179.40")
    _bill(tenant_db, "20552277", "5179.40")
    _statement_line(tenant_db, "20552277", "5179.40")
    tenant_db.commit()

    assert build_on_order(tenant_db)[0].items[0].status == STATUS_ON_STATEMENT


def test_billed_orders_do_not_count_as_committed(tenant_db):
    """Only what's un-billed is money GDX can't otherwise see."""
    _order(tenant_db, "AWAITING", "1000.00")
    _order(tenant_db, "BILLED", "2000.00")
    _bill(tenant_db, "BILLED", "2000.00")
    tenant_db.commit()

    summary = build_on_order(tenant_db)[0]
    assert summary.awaiting_bill_count == 1
    assert summary.awaiting_bill_total == Decimal("1000.00")
    assert len(summary.items) == 2      # both still listed


# --------------------------------------------------------------------------- #
# variance — ordered vs billed
# --------------------------------------------------------------------------- #
def test_billed_above_the_estimate_shows_a_positive_variance(tenant_db):
    """Real case: one order estimated $2,469.42 and billed $2,577.42. Freight
    added at ship time is the usual innocent cause — surfaced, not judged."""
    _order(tenant_db, "20476417", "2469.42")
    _bill(tenant_db, "20476417", "2577.42")
    tenant_db.commit()

    item = build_on_order(tenant_db)[0].items[0]
    assert item.variance == Decimal("108.00")


def test_billed_below_the_estimate_shows_a_negative_variance(tenant_db):
    _order(tenant_db, "X1", "1000.00")
    _bill(tenant_db, "X1", "900.00")
    tenant_db.commit()
    assert build_on_order(tenant_db)[0].items[0].variance == Decimal("-100.00")


# --------------------------------------------------------------------------- #
# shape
# --------------------------------------------------------------------------- #
def test_door_specs_ride_along_on_the_lines(tenant_db):
    _order(tenant_db, "20386640", "1708.24", lines=[
        ("Garage Door Material and Labor Pursuant to Contract",
         'CHI 2283 12\'2x11 White Short Panel 12" LHR, 1 strut', 1, "1708.24"),
    ])
    tenant_db.commit()

    line = build_on_order(tenant_db)[0].items[0].lines[0]
    assert "CHI 2283" in line.notes
    assert line.quantity == Decimal("1")


def test_accounts_are_separated_by_customer_code(tenant_db):
    _order(tenant_db, "A1", "500.00", code="ACCT-ONE")
    _order(tenant_db, "B1", "900.00", code="ACCT-TWO")
    tenant_db.commit()

    summaries = build_on_order(tenant_db)
    assert len(summaries) == 2
    assert [s.awaiting_bill_total for s in summaries] == [Decimal("900.00"), Decimal("500.00")]


def test_a_deleted_order_is_not_committed_spend(tenant_db):
    o = _order(tenant_db, "GONE", "5000.00")
    o.deleted_at = date(2026, 7, 28)
    tenant_db.commit()
    assert build_on_order(tenant_db) == []


def test_another_vendors_invoice_never_marks_an_order_billed(tenant_db):
    """An invoice number is unique per SUPPLIER — vendor_invoices enforces
    (vendor_key, invoice_number) — and the LLM rung fills that table with any
    supplier's bills. Matching on the bare number let an unrelated $42 invoice
    mark a $3,707 order billed, dropping it out of committed spend and printing
    a nonsense variance."""
    _order(tenant_db, "20635854", "3707.74")
    other = VendorInvoice(
        vendor_name_raw="A Totally Different Supplier",
        vendor_key="a-totally-different-supplier",
        invoice_number="20635854", total=Decimal("42.00"),
        source="email", extraction_method="llm",
    )
    tenant_db.add(other)
    tenant_db.commit()

    summary = build_on_order(tenant_db)[0]
    item = summary.items[0]
    assert item.status == STATUS_AWAITING_BILL
    assert item.billed_total is None
    assert item.variance is None
    assert summary.awaiting_bill_total == Decimal("3707.74")


def test_an_order_that_dropped_off_the_statement_reads_settled(tenant_db):
    """`on_statement` meaning "ever appeared" left a paid order reading as still
    owed forever, and the list never drained. Presence on the CURRENT statement
    is what "still owed" means — the same rule the account view runs on."""
    _order(tenant_db, "OLD", "1000.00")
    # An older statement asked for it...
    st_old = VendorStatement(
        vendor_name=VENDOR, vendor_code="ACME01", statement_date=date(2026, 6, 1),
        parser_name="midwest_v1", parser_version=1, raw_total=Decimal("1000.00"),
        line_count=1, status="parsed",
    )
    tenant_db.add(st_old)
    tenant_db.flush()
    tenant_db.add(VendorStatementLine(
        statement_id=st_old.id, line_no=0, vendor_invoice_no="OLD",
        amount=Decimal("1000.00"), balance=Decimal("1000.00"), aging_bucket="0-29",
    ))
    # ...the current one does not.
    _statement_line(tenant_db, "SOMETHING-ELSE", "50.00")
    tenant_db.commit()

    assert build_on_order(tenant_db)[0].items[0].status == STATUS_SETTLED


def test_finished_orders_are_capped_and_the_truncation_is_reported(tenant_db):
    """Settled and billed orders accumulate forever. Capping keeps the payload
    bounded; reporting the cut stops a partial list reading as complete."""
    from gdx_dispatch.modules.vendor_orders.onorder import MAX_FINISHED_ITEMS

    for i in range(MAX_FINISHED_ITEMS + 5):
        _order(tenant_db, f"BILLED{i:03d}", "100.00")
        _bill(tenant_db, f"BILLED{i:03d}", "100.00")
    _order(tenant_db, "STILL-OPEN", "999.00")
    tenant_db.commit()

    summary = build_on_order(tenant_db)[0]
    assert summary.finished_truncated == 5
    assert len(summary.items) == MAX_FINISHED_ITEMS + 1
    # The actionable one is never dropped, whatever its age.
    assert any(i.order_number == "STILL-OPEN" for i in summary.items)
    assert summary.awaiting_bill_total == Decimal("999.00")


def test_no_orders_yields_nothing(tenant_db):
    assert build_on_order(tenant_db) == []


def test_on_order_route_is_declared_before_the_uuid_route():
    from gdx_dispatch.routers.vendor_statements import router

    paths = [r.path for r in router.routes]
    assert paths.index("/api/vendor-statements/on-order") < paths.index(
        "/api/vendor-statements/{statement_id}"
    )
