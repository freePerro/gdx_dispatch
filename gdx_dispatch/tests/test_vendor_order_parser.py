"""Midwest order-confirmation parser.

Fixtures are SYNTHESISED: monospaced text rendered to a real PDF and read back
through pypdf, so layout extraction genuinely runs — but they are not supplier
documents. That distinction matters, because both bugs this parser has had were
found by running it against actual mail, not here. These tests pin the fixes;
they did not find them, and they would not have.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.modules.vendor_orders.parsers.midwest_order import (
    MidwestOrderParseError,
    MidwestOrderStructureError,
    parse_midwest_order,
)
from gdx_dispatch.modules.vendor_statements.parsers.midwest import VENDOR_LETTERHEAD

pytest.importorskip("weasyprint")


def _pdf(text: str) -> bytes:
    from weasyprint import HTML

    body = text.replace("&", "&amp;").replace("<", "&lt;")
    return HTML(
        string='<html><body><pre style="font-family:monospace;font-size:8pt">'
               f"{body}</pre></body></html>"
    ).write_pdf()


def _confirmation(
    *,
    order_no="20635854",
    order_date="07/23/2026",
    po="A+ Example",
    ship_to="A+ Example",
    terms="Collect On Delivery",
    lot_no="",
    called_in_by="",
    rows=(("Garage Door Material and Labor Pursuant to Contract", "2", "EA",
           "$1,853.8700", "$3,707.74", "CHI 4283 12x10 Black Long Panel, 2 struts"),),
    subtotal="$3,707.74",
    shipping="$0.00",
    tax="$0.00",
    total="$3,707.74",
) -> str:
    """Reproduces the real form's geometry — two-column rows included."""
    lines = [
        "                                      ORDER CONFIRMATION",
        f"{VENDOR_LETTERHEAD} (438)                                          ORDER NUMBER:         {order_no}",
        f"1 Example Road                                                       ORDER DATE:       {order_date}",
        "ANYTOWN, MN 55555                                                  SALES PERSON:        000001",
        "",
        "                                                                    CUSTOMER NO:         ACME01",
        "",
        " BILL TO:                                                          SHIP TO:",
        f" Example Door Co                                                   {ship_to}",
        "",
        f"                                                                  Lot No.: {lot_no}",
        "",
        f"                                                                  CALLED IN BY: {called_in_by}",
        "  CUSTOMER P.O.                                               TERMS",
        f"  {po}{' ' * max(1, 60 - len(po))}{terms}",
        "  ITEM DESCRIPTION                                               QTY ORDERED               UNIT        UNIT COST                 COST",
    ]
    for desc, qty, unit, unit_cost, cost, note in rows:
        lines.append(f"  {desc}                    {qty}                  {unit}         {unit_cost}           {cost}")
        if note:
            lines.append(f"            Notes: {note}")
    lines += [
        f"                                                                 ESTIMATED SUBTOTAL                               {subtotal}",
        f"                                                                 ESTIMATED SHIPPING                               {shipping}",
        f"                                                                 ESTIMATED TAX                                    {tax}",
        f"                                                                 ESTIMATED ORDER TOTAL                            {total}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_parses_the_header_and_the_order_number():
    o = parse_midwest_order(_pdf(_confirmation()))
    assert o.order_number == "20635854"
    assert o.order_date == date(2026, 7, 23)
    assert o.customer_code == "ACME01"
    assert o.salesperson == "000001"


def test_parses_line_items_with_the_door_spec_in_notes():
    o = parse_midwest_order(_pdf(_confirmation()))
    assert o.line_count == 1
    line = o.lines[0]
    assert line.quantity == Decimal("2")
    assert line.unit == "EA"
    assert line.unit_cost == Decimal("1853.8700")
    assert line.line_total == Decimal("3707.74")
    # The supplier bills every row as the same boilerplate; the door is in Notes.
    assert "CHI 4283 12x10 Black" in line.notes


def test_parses_estimated_totals_and_never_calls_them_owed():
    o = parse_midwest_order(_pdf(_confirmation()))
    assert o.estimated_subtotal == Decimal("3707.74")
    assert o.estimated_total == Decimal("3707.74")
    assert o.estimated_shipping == Decimal("0.00")


# --------------------------------------------------------------------------- #
# the two-column rows — whitespace splitting mangles these
# --------------------------------------------------------------------------- #
def test_reads_the_po_and_terms_as_separate_columns():
    o = parse_midwest_order(_pdf(_confirmation(po="D&E Rose City", terms="Net 30")))
    assert o.customer_po == "D&E Rose City"
    assert o.terms == "Net 30"


def test_a_po_containing_spaces_is_not_split_at_the_space():
    o = parse_midwest_order(_pdf(_confirmation(po="Openers 7.7.26", terms="Net 30")))
    assert o.customer_po == "Openers 7.7.26"


def test_ship_to_is_the_jobsite_not_the_bill_to():
    o = parse_midwest_order(_pdf(_confirmation(ship_to="SFL Trende")))
    assert o.ship_to == "SFL Trende"
    assert "Example Door Co" not in (o.ship_to or "")


# --------------------------------------------------------------------------- #
# blank fields must read as blank
# --------------------------------------------------------------------------- #
def test_empty_lot_no_does_not_swallow_the_next_line():
    """First run against real documents returned lot_no="CALLED IN BY:" — the
    label underneath it. `\\s*` matches newlines, so a blank value ran on."""
    o = parse_midwest_order(_pdf(_confirmation(lot_no="", called_in_by="")))
    assert o.lot_no is None
    assert o.called_in_by is None


def test_a_filled_lot_no_is_captured():
    """The field where a GDX job number or CHI quote number would go — this is
    what makes the job link work the day the office starts filling it in."""
    o = parse_midwest_order(_pdf(_confirmation(lot_no="QCD3839689")))
    assert o.lot_no == "QCD3839689"


# --------------------------------------------------------------------------- #
# identity: not-mine vs mine-and-broken
# --------------------------------------------------------------------------- #
def test_a_statement_is_not_an_order_confirmation():
    stmt = f"{VENDOR_LETTERHEAD}\nSTATEMENT DATE: 05/03/2026   CUSTOMER CODE: ACME01\n"
    with pytest.raises(MidwestOrderParseError) as exc:
        parse_midwest_order(_pdf(stmt))
    assert not isinstance(exc.value, MidwestOrderStructureError)


def test_another_vendors_order_confirmation_is_not_mine():
    with pytest.raises(MidwestOrderParseError) as exc:
        parse_midwest_order(_pdf("ORDER CONFIRMATION\nAcme Overhead Supply\nORDER NUMBER: 1\n"))
    assert not isinstance(exc.value, MidwestOrderStructureError)


def test_bytes_that_are_not_a_pdf_are_not_mine():
    with pytest.raises(MidwestOrderParseError) as exc:
        parse_midwest_order(b"not a pdf")
    assert not isinstance(exc.value, MidwestOrderStructureError)


def test_an_order_confirmation_with_no_order_number_is_a_structure_failure():
    text = _confirmation().replace("ORDER NUMBER:         20635854", "ORDER NUMBER:")
    with pytest.raises(MidwestOrderStructureError):
        parse_midwest_order(_pdf(text))


def test_a_row_the_pattern_missed_is_refused_not_understated():
    """The failure that matters: we parsed fewer rows than the supplier billed.

    Here the printed subtotal is $3,807.74 but the only parseable row is
    $3,707.74 — a $100 freight line the row pattern didn't catch. Recording that
    would make a commitment look smaller than it is, so the parser refuses.

    An earlier version compared printed subtotal+shipping+tax against the printed
    total, which is the vendor checking their own arithmetic — every term came
    from the document, so it could not detect a missing row at all.
    """
    with pytest.raises(MidwestOrderStructureError) as exc:
        parse_midwest_order(_pdf(_confirmation(subtotal="$3,807.74", total="$3,807.74")))
    assert "a row was missed" in str(exc.value)


def test_rounding_across_several_rows_is_tolerated():
    o = parse_midwest_order(_pdf(_confirmation(subtotal="$3,707.76", total="$3,707.76")))
    assert o.estimated_total == Decimal("3707.76")


def test_a_fourth_total_component_is_not_treated_as_an_error():
    """A discount or handling line makes subtotal+shipping+tax differ from the
    printed total legitimately. The old guard hard-failed such a document; the
    line-sum check doesn't care."""
    o = parse_midwest_order(_pdf(_confirmation(
        subtotal="$3,707.74", shipping="$0.00", tax="$0.00", total="$3,607.74")))
    assert o.estimated_total == Decimal("3607.74")
    assert o.estimated_subtotal == Decimal("3707.74")


def test_an_empty_customer_po_does_not_capture_the_table_header():
    """Scanning forward for the first non-empty slice returned "ITEM
    DESCRIPTION" — the header underneath — when the PO field was blank. That is
    the field earmarked for the GDX job link, so a bogus value there is worse
    than none."""
    o = parse_midwest_order(_pdf(_confirmation(po="", terms="Net 30")))
    assert o.customer_po is None
    assert o.terms == "Net 30"


def test_an_empty_ship_to_does_not_capture_the_row_below_it():
    o = parse_midwest_order(_pdf(_confirmation(ship_to="")))
    assert o.ship_to is None


def test_multi_line_orders_keep_each_door_spec_with_its_row():
    o = parse_midwest_order(_pdf(_confirmation(
        rows=(
            ("Garage Door Material and Labor Pursuant to Contract", "2", "EA",
             "$1,476.6700", "$2,953.34", "CHI 3285 11x12 White Micro Grooved"),
            ("Garage Door Material and Labor Pursuant to Contract", "1", "EA",
             "$1,318.4000", "$1,318.40", "CHI 3285 11x10 White Micro Grooved"),
        ),
        subtotal="$4,271.74", total="$4,271.74",
    )))
    assert o.line_count == 2
    assert "11x12" in o.lines[0].notes
    assert "11x10" in o.lines[1].notes
