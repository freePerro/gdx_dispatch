"""Extracted text must fit the column it lands in (#513).

The vendor-bill ingest failed on Postgres with::

    sqlalchemy.exc.DataError: (psycopg2.errors.StringDataRightTruncation)
    value too long for type character varying(60)

and — this is the part that made it permanent — the failure is isolated per
message by design, so the message stays un-checkpointed and is retried on
*every* sweep. One bill, failing forever.

The traceback names only the type, not the column. Settled by reproduction
rather than inference: `_TERMS` was ``TERMS:\\s*([^.]+)\\.`` and ``[^.]`` is a
NEGATED CLASS, so it matches newlines — the capture ran from "TERMS:" past
every line break to the next literal period anywhere in the document. A
realistic wrapped terms paragraph yields **215 characters** into a
``String(60)``. ``invoice_number`` is ``(\\d{4,})``, digits only, and was never
the culprit.

Both halves are here because the regex alone is not enough: bounding it to one
line still yields 64 characters on the same input. The clamp at the write is
what actually holds, and it is the only thing that covers the LLM rung, whose
output length nothing constrains at all.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.core.column_fit import column_limit as _column_limit
from gdx_dispatch.core.column_fit import fit as _fit
from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice, VendorInvoiceLine
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    _TERMS,
    ParsedInvoice,
    ParsedInvoiceLine,
    _first,
)
from gdx_dispatch.modules.vendor_invoices.service import (
    InvoiceFieldTooLong,
    _persist_parsed_invoice,
    build_lines_from_parsed,
)


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


def _parsed(**over) -> ParsedInvoice:
    base = dict(
        invoice_number="100234",
        invoice_date=date(2026, 9, 1),
        po_reference=None,
        terms="Net 30",
        net_days=30,
        due_date=date(2026, 10, 1),
        tax=Decimal("0.00"),
        shipping=Decimal("0.00"),
        total=Decimal("10.00"),
        credits_pending=Decimal("0.00"),
        amount_due=Decimal("10.00"),
        lines=[
            ParsedInvoiceLine(
                line_no=1, item_label="X", description="d",
                quantity=Decimal("1"), package=None,
                unit_price=Decimal("10.00"), line_total=Decimal("10.00"),
            )
        ],
    )
    base.update(over)
    return ParsedInvoice(**base)


def _persist(db, parsed, vendor_name_raw="Acme Supply"):
    return _persist_parsed_invoice(
        db,
        pdf_bytes=b"%PDF-1.4 fake",
        content_hash="h" * 64,
        existing_doc=None,
        parsed=parsed,
        vendor_name_raw=vendor_name_raw,
        extraction_method="parser",
        extractor_label="test",
        original_filename="invoice.pdf",
        content_type="application/pdf",
        uploaded_by="tester",
        source="upload",
    )


class TestTheRegexIsLeftAlone:
    """The tempting fix — bound the terms regex to one line — is a regression.

    `_NET_DAYS` searches the terms capture for "Net N" to derive `net_days`
    and then `due_date`. On a bill whose terms wrap, "Net 30" is on the SECOND
    line, so a one-line capture blanks an A/P aging field to tidy a string.
    The overflow is fixed at the write instead; the full capture still feeds
    the date maths and only the stored value is clamped.
    """

    WRAPPED = (
        "TERMS: Payment is due in full,\n"
        "Net 30 days from date of invoice, subject to credit approval.\n"
    )

    def test_a_wrapped_term_still_yields_net_days(self):
        captured = _first(_TERMS, self.WRAPPED)
        assert captured is not None
        assert "\n" in captured, "capture must still span lines for the date maths"
        from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import _NET_DAYS
        m = _NET_DAYS.search(captured)
        assert m is not None and m.group(1) == "30"

    def test_the_capture_can_still_exceed_the_column(self):
        """Which is exactly why the clamp, not the regex, is the fix."""
        captured = _first(_TERMS, self.WRAPPED)
        assert len(captured) > _column_limit(VendorInvoice, "terms")


class TestTheClamp:
    def test_the_limit_is_read_from_the_schema_not_hardcoded(self):
        """`build_lines_from_parsed` used to clamp with a literal `[:500]` —
        a second copy of the column length, free to drift from it."""
        assert _column_limit(VendorInvoice, "terms") == 60
        assert _column_limit(VendorInvoiceLine, "description") == 500
        assert _column_limit(VendorInvoice, "invoice_date") is None  # not a String

    def test_fit_reports_whether_it_truncated(self):
        limit = _column_limit(VendorInvoice, "terms")
        value, cut = _fit(VendorInvoice, "terms", "x" * (limit + 40))
        assert cut is True and len(value) == limit
        value, cut = _fit(VendorInvoice, "terms", "Net 30")
        assert cut is False and value == "Net 30"
        assert _fit(VendorInvoice, "terms", None) == (None, False)

    def test_an_overlong_terms_is_stored_not_raised(self, tenant_db):
        """THE #513 regression. Before: DataError, and the ingest died.

        215 characters is what the real parser produced from a wrapped terms
        paragraph — the exact shape that failed on prod.
        """
        long_terms = (
            "Net 30 days from date of invoice, subject to credit approval and "
            "our standard terms and conditions of sale as published on our "
            "website, late payments accrue a service charge"
        )
        assert len(long_terms) > 60
        result = _persist(tenant_db, _parsed(terms=long_terms))

        assert result.created is True
        assert len(result.invoice.terms) <= _column_limit(VendorInvoice, "terms")
        assert result.invoice.terms == long_terms[:60]

    def test_the_truncation_is_recorded_not_swallowed(self, tenant_db):
        """A truncated value is a fact about the record. Silently storing a
        shortened one and saying nothing is the class this repo treats as a
        defect of the highest order."""
        result = _persist(tenant_db, _parsed(terms="N" * 200))
        assert "truncated to fit" in (result.invoice.notes or "")
        assert "terms" in (result.invoice.notes or "")

    def test_a_normal_bill_gets_no_truncation_note(self, tenant_db):
        result = _persist(tenant_db, _parsed())
        assert "truncated" not in (result.invoice.notes or "")

    def test_the_llm_rung_is_covered_too(self, tenant_db):
        """The parser regex is bounded now; an LLM's output length is not
        constrained by anything at all, and it feeds the same columns."""
        result = _persist(tenant_db, _parsed(po_reference="P" * 400),
                          vendor_name_raw="V" * 400)
        assert len(result.invoice.vendor_name_raw) <= _column_limit(
            VendorInvoice, "vendor_name_raw")
        assert len(result.invoice.po_reference) <= _column_limit(
            VendorInvoice, "po_reference")

    def test_line_fields_are_clamped_from_the_schema(self):
        parsed = _parsed(lines=[
            ParsedInvoiceLine(
                line_no=1, item_label="L" * 400, description="D" * 900,
                quantity=Decimal("1"), package=None,
                unit_price=Decimal("1.00"), line_total=Decimal("1.00"),
            )
        ])
        line = build_lines_from_parsed(parsed)[0]
        assert len(line.item_label) == _column_limit(VendorInvoiceLine, "item_label")
        assert len(line.description) == _column_limit(VendorInvoiceLine, "description")


class TestTheKeyIsNotClamped:
    """The dedup key is the one field that must NOT be shortened.

    Clamping a key does not trim a value, it MERGES records. Two genuinely
    different bills whose numbers share the first 60 characters would collapse
    into one row, and the second would come back
    `created=False, duplicate_reason="vendor_invoice_number"` — a
    success-shaped result for a payable that was never stored. Silently losing
    a bill is worse than the loud DataError this issue is about, and it is
    reachable exactly where clamping is most needed: the LLM rung, whose output
    length nothing bounds.
    """

    def test_two_different_long_numbers_do_not_collapse_into_one(self, tenant_db):
        """The falsifier. Before this, B's total silently replaced nothing and
        vanished; now the extraction is refused instead."""
        limit = _column_limit(VendorInvoice, "invoice_number")
        shared = "9" * limit
        a, b = shared + "AAAAA", shared + "BBBBB"

        with pytest.raises(InvoiceFieldTooLong):
            _persist(tenant_db, _parsed(invoice_number=a))
        tenant_db.rollback()
        with pytest.raises(InvoiceFieldTooLong):
            _persist(tenant_db, _parsed(invoice_number=b, total=Decimal("999.99")))
        tenant_db.rollback()

        # Neither was silently merged into the other, and neither was invented.
        assert tenant_db.query(VendorInvoice).count() == 0

    def test_the_refusal_is_a_parse_error_so_the_ladder_routes_it(self):
        """A subclass of the existing parse error, so the parser -> LLM ->
        manual-queue ladder handles it without a new branch."""
        from gdx_dispatch.modules.vendor_invoices.service import InvoiceParseError

        assert issubclass(InvoiceFieldTooLong, InvoiceParseError)

    def test_a_normal_invoice_number_is_untouched(self, tenant_db):
        result = _persist(tenant_db, _parsed(invoice_number="100234"))
        assert result.created is True
        assert result.invoice.invoice_number == "100234"


class TestTheSiblingModule:
    """The statement ingest writes the same shape of text into the same shape
    of columns, from the same document family, through the same sweep — and
    clamped nothing at all. Found by this issue's sibling sweep.
    """

    def _parsed_statement(self, **over):
        from datetime import date as _date

        from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
            MidwestParsedLine,
            MidwestParseResult,
        )

        line = MidwestParsedLine(
            line_no=1,
            invoice_no=over.pop("invoice_no", "100234"),
            job_no=over.pop("job_no", "J-1"),
            rep="R",
            line_date=_date(2026, 9, 1),
            amount=Decimal("10.00"),
            balance=Decimal("10.00"),
            aging_0_29=Decimal("10.00"),
            aging_30_59=Decimal("0"), aging_60_89=Decimal("0"),
            aging_90_119=Decimal("0"), aging_120_plus=Decimal("0"),
            retainage=Decimal("0"),
            po_ref=over.pop("po_ref", "PO-1"),
            description=over.pop("description", "d"),
            raw_text="raw",
        )
        return MidwestParseResult(
            statement_date=_date(2026, 9, 1),
            customer_code=over.pop("customer_code", "C1"),
            raw_total=Decimal("10.00"),
            lines=[line],
        )

    def test_overlong_statement_fields_are_clamped(self, tenant_db):
        from gdx_dispatch.modules.vendor_statements.models import (
            VendorStatement,
            VendorStatementLine,
        )
        from gdx_dispatch.modules.vendor_statements.service import (
            _persist_parsed_statement,
        )

        parsed = self._parsed_statement(
            customer_code="C" * 300,
            invoice_no="9" * 300,
            job_no="J" * 300,
            po_ref="P" * 300,
            description="D" * 900,
        )
        _persist_parsed_statement(
            tenant_db,
            pdf_bytes=b"%PDF-1.4 fake",
            content_hash="s" * 64,
            existing_doc=None,
            parsed=parsed,
            original_filename="statement.pdf",
            content_type="application/pdf",
            uploaded_by="tester",
            source="upload",
        )
        stmt = tenant_db.query(VendorStatement).one()
        line = tenant_db.query(VendorStatementLine).one()

        assert len(stmt.vendor_code) == _column_limit(VendorStatement, "vendor_code")
        for field_name in ("vendor_invoice_no", "vendor_job_no", "po_ref", "description"):
            limit = _column_limit(VendorStatementLine, field_name)
            assert len(getattr(line, field_name)) == limit, field_name


class TestTheThirdDestination:
    """The same message stream routes to THREE persist paths — invoices,
    statements and orders. Orders was missed on the first pass: same shape,
    same DB, same un-checkpointed sweep, same forever-failure.
    """

    def _parsed_order(self, **over):
        from datetime import date as _date

        from gdx_dispatch.modules.vendor_orders.parsers.midwest_order import (
            ParsedOrder,
            ParsedOrderLine,
        )

        return ParsedOrder(
            order_number=over.pop("order_number", "SO-1001"),
            order_date=_date(2026, 9, 1),
            customer_code=over.pop("customer_code", "C1"),
            salesperson=over.pop("salesperson", "S"),
            ship_to=over.pop("ship_to", "1 Main St"),
            customer_po=over.pop("customer_po", "PO-1"),
            terms=over.pop("terms", "Net 30"),
            lot_no=over.pop("lot_no", "L-1"),
            called_in_by=over.pop("called_in_by", "Someone"),
            estimated_subtotal=Decimal("10.00"),
            estimated_shipping=Decimal("0.00"),
            estimated_tax=Decimal("0.00"),
            estimated_total=Decimal("10.00"),
            lines=[
                ParsedOrderLine(
                    line_no=1,
                    description=over.pop("description", "d"),
                    notes=None,
                    quantity=Decimal("1"),
                    unit="EA",
                    unit_cost=Decimal("10.00"),
                    line_total=Decimal("10.00"),
                )
            ],
        )

    def _ingest(self, db, parsed, monkeypatch):
        from gdx_dispatch.modules.vendor_orders import service as order_service

        monkeypatch.setattr(order_service, "parse_midwest_order", lambda _b: parsed)
        return order_service.ingest_midwest_order(
            db,
            pdf_bytes=b"%PDF-1.4 fake",
            original_filename="order.pdf",
            content_type="application/pdf",
            uploaded_by="tester",
            source="upload",
        )

    def test_overlong_order_fields_are_clamped_and_recorded(
        self, tenant_db, monkeypatch
    ):
        from gdx_dispatch.modules.vendor_orders.models import VendorOrder, VendorOrderLine

        parsed = self._parsed_order(
            ship_to="S" * 400, terms="T" * 400, customer_po="P" * 400,
            lot_no="L" * 400, called_in_by="B" * 400, description="D" * 900,
        )
        self._ingest(tenant_db, parsed, monkeypatch)

        order = tenant_db.query(VendorOrder).one()
        for field_name in ("ship_to", "terms", "customer_po", "lot_no", "called_in_by"):
            assert len(getattr(order, field_name)) == _column_limit(
                VendorOrder, field_name), field_name
        assert "truncated to fit" in (order.notes or "")

        line = tenant_db.query(VendorOrderLine).one()
        assert len(line.description) == _column_limit(VendorOrderLine, "description")

    def test_an_overlong_order_number_is_refused_not_clamped(
        self, tenant_db, monkeypatch
    ):
        """The key, again. Clamping it would merge two different orders."""
        from gdx_dispatch.modules.vendor_orders.models import VendorOrder
        from gdx_dispatch.modules.vendor_orders.service import OrderFieldTooLong

        limit = _column_limit(VendorOrder, "order_number")
        with pytest.raises(OrderFieldTooLong):
            self._ingest(
                tenant_db,
                self._parsed_order(order_number="9" * (limit + 10)),
                monkeypatch,
            )


class TestTheGuardCannotFailOpen:
    def test_an_unknown_field_raises_instead_of_silently_not_clamping(self):
        """A guard that quietly declines to guard is worse than none: a typo,
        a rename or a mapped_column alias would yield no clamp, no truncation
        flag and no error."""
        from gdx_dispatch.core.column_fit import UnknownColumn, fit_all

        with pytest.raises(UnknownColumn):
            _fit(VendorInvoice, "invoice_numbr", "X" * 500)
        with pytest.raises(UnknownColumn):
            fit_all(VendorInvoice, {"nope": "x"})
