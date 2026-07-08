"""Vendor invoice parser — unit tests.

Deterministic tests drive the pure ``parse_invoice_text`` with a SYNTHETIC
layout string (entirely fabricated data — no real invoice content lives in the
repo). A separate end-to-end test parses a real sample PDF only when
``VENDOR_INVOICE_SAMPLE_PDF`` points at one, and skips otherwise.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    MidwestInvoiceParseError,
    parse_invoice_text,
    parse_midwest_invoice,
)

# A fabricated invoice in the exact layout shape pypdf produces for this
# vendor's retail-sale PDF: right-aligned header, Sold/Deliver two-column,
# generic "Garage Door Material" item rows each followed by a Notes row (with
# one wrapped across two physical lines), then the totals block and terms.
SYNTHETIC_INVOICE = """\
                                                              Retail Sale
                                                                 INVOICE
                                                                99001234

                                              Invoice Date: 01/15/2026

          Sold To:                                            Deliver To:

              Example Contractor LLC                          Example Customer
              123 Example St                                  123 Example St
              EXAMPLETOWN, ZZ 00000                           EXAMPLETOWN, ZZ 00000

                                                              PO#: Example Job A

Units / Coverage        Inventory Item Description       Package    Unit Price          Price

      2                 Garage Door Material                2      $100.0000           $200.00

       Notes: MODEL-A 9x7 White Example Panel 1 strut,
Example Bracket
      1                 Garage Door Material                1      $50.0000             $50.00

       Notes: MODEL-B 16x7 White Example Panel 2 strut

                                                    Sales Tax                          $0.00
                                                    Shipping & Handling               $25.00
                                                    Total                            $275.00
                                                    Credits Pending                    $0.00
                                                    Amount Due                       $275.00

                               TERMS: Net 30. Please reference invoice number with payment.
"""


def test_header_fields():
    r = parse_invoice_text(SYNTHETIC_INVOICE)
    assert r.invoice_number == "99001234"
    assert r.invoice_date == date(2026, 1, 15)
    assert r.po_reference == "Example Job A"
    assert r.terms == "Net 30"
    assert r.net_days == 30
    assert r.due_date == date(2026, 2, 14)


def test_totals_and_invariant():
    r = parse_invoice_text(SYNTHETIC_INVOICE)
    assert r.tax == Decimal("0.00")
    assert r.shipping == Decimal("25.00")
    assert r.total == Decimal("275.00")
    assert r.amount_due == Decimal("275.00")
    assert r.subtotal == Decimal("250.00")
    # subtotal + tax + shipping == total
    assert r.invariant_discrepancy() == Decimal("0.00")


def test_line_items_and_notes_description():
    r = parse_invoice_text(SYNTHETIC_INVOICE)
    assert r.line_count == 2

    l0 = r.lines[0]
    assert l0.quantity == Decimal("2")
    assert l0.unit_price == Decimal("100.0000")
    assert l0.line_total == Decimal("200.00")
    assert l0.item_label == "Garage Door Material"
    # The real (matchable) description comes from the Notes row, wrapped lines
    # merged into one.
    assert l0.description == "MODEL-A 9x7 White Example Panel 1 strut, Example Bracket"
    assert l0.line_math_discrepancy() == Decimal("0")

    l1 = r.lines[1]
    assert l1.quantity == Decimal("1")
    assert l1.line_total == Decimal("50.00")
    assert l1.description == "MODEL-B 16x7 White Example Panel 2 strut"


def test_invariant_mismatch_is_reported_not_raised():
    """A wrong Total is surfaced via invariant_discrepancy (the service routes
    it to the manual queue) — the parser does not raise."""
    broken = SYNTHETIC_INVOICE.replace("Total                            $275.00",
                                       "Total                            $999.00")
    r = parse_invoice_text(broken)
    assert r.total == Decimal("999.00")
    assert r.invariant_discrepancy() == Decimal("724.00")


def test_line_math_discrepancy_detected_when_header_still_balances():
    """A quantity/price misread that keeps line_total (and thus the header)
    correct is caught by the per-line qty*unit check — the service uses this to
    route to the manual queue instead of feeding a wrong number to inventory."""
    text = """\
Units / Coverage        Inventory Item Description       Package    Unit Price          Price

      2                 Garage Door Material                2      $100.0000           $150.00

       Notes: MODEL-X

                                                    Sales Tax                          $0.00
                                                    Shipping & Handling                $0.00
                                                    Total                            $150.00
                                                    Amount Due                       $150.00

                               TERMS: Net 30.
"""
    # Fake header/date so the required anchors are present.
    text = "INVOICE\n77770000\nInvoice Date: 01/15/2026\nPO#: X\n" + text
    r = parse_invoice_text(text)
    # Header balances (subtotal 150 + 0 + 0 == 150)…
    assert r.invariant_discrepancy() == Decimal("0.00")
    # …but the single line's qty*unit (200) != printed total (150).
    assert r.lines[0].line_math_discrepancy() == Decimal("50.0000")


def test_rejects_non_invoice_text():
    with pytest.raises(MidwestInvoiceParseError):
        parse_invoice_text("just some text, not an invoice at all")


def test_rejects_empty():
    with pytest.raises(MidwestInvoiceParseError):
        parse_invoice_text("")
    with pytest.raises(MidwestInvoiceParseError):
        parse_midwest_invoice(b"")


def test_rejects_non_pdf_bytes():
    with pytest.raises(MidwestInvoiceParseError):
        parse_midwest_invoice(b"%PDF-1.4\n%not really\n")


# --------------------------------------------------------------------------- #
# End-to-end against a real sample PDF (only when provided; never committed)
# --------------------------------------------------------------------------- #
def _sample_pdf() -> bytes:
    path = os.getenv("VENDOR_INVOICE_SAMPLE_PDF", "").strip()
    if not path or not Path(path).exists():
        pytest.skip("set VENDOR_INVOICE_SAMPLE_PDF to a real invoice PDF to run this")
    return Path(path).read_bytes()


def test_real_pdf_parses_end_to_end():
    r = parse_midwest_invoice(_sample_pdf())
    # Structural expectations that hold for any well-formed invoice of this
    # layout — no hard-coded private values.
    assert r.invoice_number
    assert r.total > 0
    assert r.line_count >= 1
    assert r.invariant_discrepancy() <= Decimal("0.02")
    for ln in r.lines:
        assert ln.line_math_discrepancy() <= Decimal("0.02")
