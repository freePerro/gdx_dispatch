"""The parser's two failure kinds, against REAL PDF bytes.

The email rung routes on the difference between "this isn't a Midwest
statement" and "this is one and I couldn't read it": the first falls through
to the next parser, the second is a dropped document that has to be shouted
about. If that distinction is wrong at the parser, every routing decision
built on it is wrong too — so these tests build actual PDFs and run the real
``parse_midwest_statement`` rather than a stub.

The sibling parser test needs Doug's original statement (absent from the repo,
so it skips). These synthesize the layout instead, which costs the fidelity of
a genuine vendor PDF but buys coverage that always runs.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    MidwestParseError,
    MidwestStatementStructureError,
    parse_midwest_statement,
)

pytest.importorskip("weasyprint")

HEADER = "Midwest Wholesale Doors\nSTATEMENT DATE: 05/03/2026     CUSTOMER CODE: GDX01"
LINE_A = "100493 AB 04/12/2026 $1,200.00 $1,200.00 $1,200.00 $0.00 $0.00 $0.00 $0.00 $0.00"
LINE_B = "900112 PO-4471 / 58 8x7 ThermoGuard door"


def _pdf(text: str) -> bytes:
    """Render monospaced text to a PDF whose layout extraction returns it
    verbatim (verified round-trip)."""
    from weasyprint import HTML

    body = text.replace("&", "&amp;").replace("<", "&lt;")
    return HTML(
        string=f'<html><body><pre style="font-family:monospace;font-size:9pt">{body}</pre></body></html>'
    ).write_pdf()


# --------------------------------------------------------------------------- #
# the happy path, so the failure cases below mean something
# --------------------------------------------------------------------------- #
def test_a_well_formed_statement_parses_from_real_pdf_bytes():
    result = parse_midwest_statement(_pdf(f"{HEADER}\n{LINE_A}\n{LINE_B}\n"))

    assert result.statement_date == date(2026, 5, 3)
    assert result.customer_code == "GDX01"
    assert result.line_count == 1
    line = result.lines[0]
    assert line.invoice_no == "100493"
    assert line.job_no == "900112"
    assert line.amount == Decimal("1200.00")
    assert line.po_ref == "PO-4471"
    assert line.description == "8x7 ThermoGuard door"
    assert result.raw_total == Decimal("1200.00")


# --------------------------------------------------------------------------- #
# "not mine" — plain MidwestParseError, caller keeps looking
# --------------------------------------------------------------------------- #
def test_another_vendors_pdf_is_not_mine_not_a_structure_failure():
    with pytest.raises(MidwestParseError) as exc:
        parse_midwest_statement(_pdf("Acme Overhead Door Supply\nInvoice 5512\nTotal $900.00\n"))
    assert not isinstance(exc.value, MidwestStatementStructureError)


def test_bytes_that_are_not_a_pdf_at_all_are_not_mine():
    with pytest.raises(MidwestParseError) as exc:
        parse_midwest_statement(b"this is not a pdf")
    assert not isinstance(exc.value, MidwestStatementStructureError)


def test_empty_bytes_are_not_mine():
    with pytest.raises(MidwestParseError) as exc:
        parse_midwest_statement(b"")
    assert not isinstance(exc.value, MidwestStatementStructureError)


# --------------------------------------------------------------------------- #
# "mine, and I lost it" — the subclass, so the caller can shout
# --------------------------------------------------------------------------- #
def test_detail_line_with_a_different_branch_code_is_a_structure_failure():
    """The parser anchors on branch code 58. A statement from another branch
    is unmistakably Midwest's and must not be mistaken for a stranger's PDF."""
    other_branch = LINE_B.replace("/ 58", "/ 61")
    with pytest.raises(MidwestStatementStructureError):
        parse_midwest_statement(_pdf(f"{HEADER}\n{LINE_A}\n{other_branch}\n"))


def test_aging_row_with_no_detail_line_is_a_structure_failure():
    with pytest.raises(MidwestStatementStructureError):
        parse_midwest_statement(_pdf(f"{HEADER}\n{LINE_A}\n"))


def test_short_job_number_is_a_structure_failure():
    with pytest.raises(MidwestStatementStructureError):
        parse_midwest_statement(_pdf(f"{HEADER}\n{LINE_A}\n9001 PO-4471 / 58 8x7 door\n"))


def test_midwest_letterhead_with_no_line_items_is_a_structure_failure():
    """Their letterhead, none of their rows — we were handed a statement (or
    something wearing one) and produced nothing. Not a stranger's document."""
    with pytest.raises(MidwestStatementStructureError):
        parse_midwest_statement(_pdf(f"{HEADER}\nNo open items.\n"))


def test_structure_failures_are_still_catchable_as_the_base_error():
    """Existing callers that catch MidwestParseError must keep working — the
    manual upload path's 422 depends on it."""
    with pytest.raises(MidwestParseError):
        parse_midwest_statement(_pdf(f"{HEADER}\n{LINE_A}\n"))
