"""Sprint vendor-statement-recon slice 1 — Midwest PDF parser unit test.

Uses the real sample statement Doug uploaded 2026-05-04 (27 line items,
$66,669.41 total). Asserts header parse + line count + spot-checks specific
rows that exercise customer-name, inventory ("Stock"), and ambiguous
("Add-ons") description shapes.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    MidwestParseError,
    parse_midwest_statement,
)


SAMPLE_PDF = Path("/path/to/sample-files/cs_master (41).PDF")


def _sample_bytes() -> bytes:
    if not SAMPLE_PDF.exists():
        pytest.skip(f"sample PDF not present at {SAMPLE_PDF}")
    return SAMPLE_PDF.read_bytes()


def test_parse_header_fields():
    result = parse_midwest_statement(_sample_bytes())
    assert result.statement_date == date(2026, 5, 3)
    assert result.customer_code == "GARA01"


def test_parse_line_count_and_total():
    result = parse_midwest_statement(_sample_bytes())
    # Statement footer in the PDF: 27 line items totalling $66,669.41
    assert result.line_count == 27
    assert result.raw_total == Decimal("66669.41")


def test_first_line_lynn_jim_liepold():
    result = parse_midwest_statement(_sample_bytes())
    line = result.lines[0]
    assert line.invoice_no == "19415997"
    assert line.job_no == "7698680"
    assert line.line_date == date(2025, 11, 22)
    assert line.amount == Decimal("6543.52")
    assert line.balance == Decimal("2543.52")
    assert line.po_ref == "438-4469"
    assert line.description == "LYNN & JIM LIEPOLD"
    assert line.aging_bucket == "120+"


def test_inventory_shaped_line_stock():
    result = parse_midwest_statement(_sample_bytes())
    stock = next((ln for ln in result.lines if ln.description == "Stock"), None)
    assert stock is not None, "expected a 'Stock' line in sample statement"
    assert stock.invoice_no == "19679485"
    assert stock.amount == Decimal("504.26")


def test_ambiguous_addons_line_kept():
    """Bare 'Add-ons' lines are ambiguous (slice 2 will classify); slice 1
    just makes sure we don't drop them."""
    result = parse_midwest_statement(_sample_bytes())
    addons = [ln for ln in result.lines if ln.description.lower().startswith("add")]
    assert len(addons) >= 3


def test_aging_bucket_distribution_matches_statement_footer():
    """Statement footer aging totals: 60-89=$5,691.14, 90-119=$40,290.24, 120+=$20,688.03."""
    result = parse_midwest_statement(_sample_bytes())
    sums = {"0-29": Decimal("0"), "30-59": Decimal("0"), "60-89": Decimal("0"),
            "90-119": Decimal("0"), "120+": Decimal("0"), "retainage": Decimal("0")}
    for ln in result.lines:
        sums["0-29"] += ln.aging_0_29
        sums["30-59"] += ln.aging_30_59
        sums["60-89"] += ln.aging_60_89
        sums["90-119"] += ln.aging_90_119
        sums["120+"] += ln.aging_120_plus
        sums["retainage"] += ln.retainage
    assert sums["60-89"] == Decimal("5691.14")
    assert sums["90-119"] == Decimal("40290.24")
    assert sums["120+"] == Decimal("20688.03")


def test_rejects_non_midwest_pdf():
    """Generic bytes that aren't a Midwest statement get rejected."""
    fake = b"%PDF-1.4\n% not midwest\n"
    with pytest.raises(MidwestParseError):
        parse_midwest_statement(fake)


def test_rejects_empty_bytes():
    with pytest.raises(MidwestParseError):
        parse_midwest_statement(b"")
