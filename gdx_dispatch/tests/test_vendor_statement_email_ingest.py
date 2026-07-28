"""Email-path vendor statement ingest — dedup semantics against a real DB.

``ingest_midwest_statement`` is the rung the Outlook bridge calls. It differs
from the manual upload path in exactly one way: a collision is a returned
duplicate, not a raised error, because there is no human on this path to show
a 409 to. These tests pin the three collision outcomes plus the happy path.

The dedup cases stub the parser so a collision is the only variable. The
end-to-end block at the bottom stubs nothing but Microsoft Graph — real PDF
bytes, real parser, real tables — so the wiring between them is exercised
rather than assumed.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.models.tenant_models import Document
from gdx_dispatch.modules.vendor_statements import service as svc
from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine
from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    MidwestParsedLine,
    MidwestParseError,
    MidwestParseResult,
)


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    yield


def _line(n=0, invoice="100001", amount="100.00"):
    return MidwestParsedLine(
        line_no=n,
        invoice_no=invoice,
        job_no="900001",
        rep="AB",
        line_date=date(2026, 5, 1),
        amount=Decimal(amount),
        balance=Decimal(amount),
        aging_0_29=Decimal(amount),
        aging_30_59=Decimal("0.00"),
        aging_60_89=Decimal("0.00"),
        aging_90_119=Decimal("0.00"),
        aging_120_plus=Decimal("0.00"),
        retainage=Decimal("0.00"),
        po_ref="PO-1",
        description="8x7 door",
        raw_text="raw",
    )


def _result(statement_date=date(2026, 5, 3), code="GDX01", lines=None):
    lines = _lines_or_default(lines)
    return MidwestParseResult(
        statement_date=statement_date,
        customer_code=code,
        raw_total=sum((ln.balance for ln in lines), Decimal("0.00")),
        lines=lines,
    )


def _lines_or_default(lines):
    return lines if lines is not None else [_line()]


def _stub_parser(monkeypatch, result):
    monkeypatch.setattr(svc, "parse_midwest_statement", lambda _b: result)


def _ingest(db, pdf_bytes=b"%PDF-1.4 statement", name="statement.pdf"):
    return svc.ingest_midwest_statement(
        db,
        pdf_bytes=pdf_bytes,
        original_filename=name,
        content_type="application/pdf",
        uploaded_by="outlook",
    )


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_email_ingest_creates_statement_lines_and_document(tenant_db, monkeypatch):
    _stub_parser(monkeypatch, _result(lines=[_line(0, "100001"), _line(1, "100002")]))

    result = _ingest(tenant_db)
    tenant_db.commit()

    assert result.created is True
    assert result.duplicate_reason is None
    assert result.statement.vendor_name == "Midwest Wholesale Doors"
    assert result.statement.statement_date == date(2026, 5, 3)
    assert result.statement.line_count == 2
    # Provenance is the whole point of the feature being visible.
    assert result.statement.source == "email"
    assert result.document is not None
    assert len(result.document.content_hash) == 64
    assert tenant_db.query(VendorStatementLine).filter_by(
        statement_id=result.statement.id
    ).count() == 2


def test_manual_upload_path_still_stamps_source_upload(tenant_db, monkeypatch):
    """The two doors must be distinguishable — otherwise 'is the automation
    working?' is unanswerable from the statements list."""
    _stub_parser(monkeypatch, _result())
    result = svc.upload_midwest_statement(
        tenant_db,
        pdf_bytes=b"%PDF-1.4 manual",
        original_filename="cs_master.PDF",
        content_type="application/pdf",
        uploaded_by="user-1",
    )
    tenant_db.commit()
    assert result.statement.source == "upload"


# --------------------------------------------------------------------------- #
# collision 1 — identical bytes
# --------------------------------------------------------------------------- #
def test_same_bytes_twice_returns_the_same_statement(tenant_db, monkeypatch):
    _stub_parser(monkeypatch, _result())

    first = _ingest(tenant_db)
    tenant_db.commit()
    second = _ingest(tenant_db)
    tenant_db.commit()

    assert second.created is False
    assert second.duplicate_reason == "content_hash"
    assert second.statement.id == first.statement.id
    assert tenant_db.query(VendorStatement).count() == 1


# --------------------------------------------------------------------------- #
# collision 2 — same period, different bytes (the forward / re-print case)
# --------------------------------------------------------------------------- #
def test_reprint_of_a_known_period_does_not_double_the_balance(tenant_db, monkeypatch):
    """The vendor forwards the same month again. Byte-different, same statement
    — two rows here would double-count the balance in reconciliation."""
    _stub_parser(monkeypatch, _result(statement_date=date(2026, 5, 3)))
    first = _ingest(tenant_db, b"%PDF original")
    tenant_db.commit()

    second = _ingest(tenant_db, b"%PDF a forwarded re-print, different bytes")
    tenant_db.commit()

    assert second.created is False
    assert second.duplicate_reason == "statement_period"
    assert second.statement.id == first.statement.id
    assert tenant_db.query(VendorStatement).count() == 1


def test_a_different_month_is_not_a_duplicate(tenant_db, monkeypatch):
    _stub_parser(monkeypatch, _result(statement_date=date(2026, 5, 3)))
    _ingest(tenant_db, b"%PDF may")
    tenant_db.commit()

    _stub_parser(monkeypatch, _result(statement_date=date(2026, 6, 3)))
    june = _ingest(tenant_db, b"%PDF june")
    tenant_db.commit()

    assert june.created is True
    assert tenant_db.query(VendorStatement).count() == 2


def test_undated_statements_are_never_collapsed_together(tenant_db, monkeypatch):
    """A statement whose date didn't parse has no identity — treating two of
    them as the same row would silently lose a real document."""
    _stub_parser(monkeypatch, _result(statement_date=None))
    _ingest(tenant_db, b"%PDF undated one")
    tenant_db.commit()
    second = _ingest(tenant_db, b"%PDF undated two")
    tenant_db.commit()

    assert second.created is True
    assert tenant_db.query(VendorStatement).count() == 2


# --------------------------------------------------------------------------- #
# collision 3 — the PDF exists as a Document but was never parsed as a statement
# --------------------------------------------------------------------------- #
def test_pdf_already_filed_as_a_document_still_becomes_a_statement(tenant_db, monkeypatch):
    """The manual path raises a 409 here. On the email path that would mean the
    statement never lands at all — so the Document is reused and the statement
    is created against it."""
    pdf = b"%PDF-1.4 already filed"
    existing = Document(
        filename="prior.pdf",
        original_name="prior.pdf",
        file_size=len(pdf),
        content_type="application/pdf",
        uploaded_by="someone",
        title="Filed earlier as a job attachment",
        content_hash=svc.compute_sha256(pdf),
    )
    tenant_db.add(existing)
    tenant_db.commit()

    _stub_parser(monkeypatch, _result())
    result = _ingest(tenant_db, pdf)
    tenant_db.commit()

    assert result.created is True
    assert result.statement.document_id == existing.id      # reused, no re-store
    assert tenant_db.query(Document).filter_by(content_hash=existing.content_hash).count() == 1

    # And the manual path keeps its 409 contract on the same collision.
    with pytest.raises(svc.DuplicateDocumentError):
        svc.upload_midwest_statement(
            tenant_db,
            pdf_bytes=pdf,
            original_filename="prior.pdf",
            content_type="application/pdf",
            uploaded_by="user-1",
        )


# --------------------------------------------------------------------------- #
# non-statements
# --------------------------------------------------------------------------- #
def test_unparseable_pdf_raises_so_the_caller_climbs_to_the_next_rung(tenant_db, monkeypatch):
    def _boom(_b):
        raise MidwestParseError("not a midwest statement")

    monkeypatch.setattr(svc, "parse_midwest_statement", _boom)

    with pytest.raises(MidwestParseError):
        _ingest(tenant_db, b"a scanned invoice")
    assert tenant_db.query(VendorStatement).count() == 0


def test_empty_file_raises_without_touching_the_database(tenant_db):
    with pytest.raises(MidwestParseError):
        _ingest(tenant_db, b"")
    assert tenant_db.query(VendorStatement).count() == 0


# --------------------------------------------------------------------------- #
# end-to-end: real PDF bytes, real parser, real DB, real rung routing.
#
# Everything above stubs the parser to isolate dedup logic. These don't stub
# anything except Microsoft Graph — the only piece that can't run locally — so
# the wiring between the ladder, the parser, and the tables is actually
# exercised rather than assumed.
# --------------------------------------------------------------------------- #
pytest.importorskip("weasyprint")

_HEADER = "Midwest Wholesale Doors\nSTATEMENT DATE: 06/03/2026     CUSTOMER CODE: GDX01"
_ROW_A = "100493 AB 05/12/2026 $1,200.00 $1,200.00 $1,200.00 $0.00 $0.00 $0.00 $0.00 $0.00"
_ROW_B = "900112 PO-4471 / 58 8x7 ThermoGuard door"


def _real_statement_pdf(text=None) -> bytes:
    from weasyprint import HTML

    body = text or f"{_HEADER}\n{_ROW_A}\n{_ROW_B}\n"
    return HTML(
        string='<html><body><pre style="font-family:monospace;font-size:9pt">'
               f"{body}</pre></body></html>"
    ).write_pdf()


def test_end_to_end_a_real_pdf_becomes_a_statement_row(tenant_db):
    """No parser stub: real bytes in, real rows out."""
    result = svc.ingest_midwest_statement(
        tenant_db,
        pdf_bytes=_real_statement_pdf(),
        original_filename="cs_master.PDF",
        content_type="application/pdf",
        uploaded_by="outlook",
    )
    tenant_db.commit()

    assert result.created is True
    assert result.statement.source == "email"
    assert result.statement.statement_date == date(2026, 6, 3)
    assert result.statement.vendor_code == "GDX01"
    assert result.statement.raw_total == Decimal("1200.00")
    line = tenant_db.query(VendorStatementLine).filter_by(
        statement_id=result.statement.id
    ).one()
    assert line.vendor_invoice_no == "100493"
    assert line.po_ref == "PO-4471"


def test_end_to_end_through_the_full_rung_ladder(tenant_db, monkeypatch):
    """The whole path the mailbox actually takes: an allowlisted sender's PDF
    attachment walks the ladder, misses the bill parser, and lands as a
    statement — with only Graph faked."""
    from gdx_dispatch.modules.outlook import vendor_bill_ingest as vbi

    pdf = _real_statement_pdf()

    class _GC:
        def list_attachments(self, mid):
            return [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "id": "att-1", "contentType": "application/pdf",
                "name": "cs_master.PDF",
            }]

        def download_attachment(self, mid, aid):
            return pdf

    message = {
        "id": "AAMk-real",
        "hasAttachments": True,
        "from": {"emailAddress": {"address": "carol.akes@supplier-domain.test"}},
    }

    totals = vbi.ingest_message_attachments(
        tenant_db, _GC(), message, ["supplier-domain.test"], llm_client=None
    )
    tenant_db.commit()

    assert totals["statements"] == 1
    assert totals["ingested"] == 0          # not a payable
    assert totals["unparseable"] == 0       # not dropped
    assert totals["statement_unparseable"] == 0
    assert totals["errors"] == 0

    stored = tenant_db.query(VendorStatement).one()
    assert stored.source == "email"
    assert stored.statement_date == date(2026, 6, 3)
    assert stored.line_count == 1

    # Re-delivery of the same mail is a no-op, not a second statement.
    again = vbi.ingest_message_attachments(
        tenant_db, _GC(), message, ["supplier-domain.test"], llm_client=None
    )
    tenant_db.commit()
    assert again["statement_duplicate"] == 1
    assert again["statements"] == 0
    assert tenant_db.query(VendorStatement).count() == 1


def test_end_to_end_a_drifted_statement_is_reported_not_swallowed(tenant_db):
    """Their letterhead, a detail line the parser can't read: the document is
    dropped (nothing to record) but it must be attributable, not filed with
    the junk attachments."""
    from gdx_dispatch.modules.outlook import vendor_bill_ingest as vbi

    drifted = _real_statement_pdf(f"{_HEADER}\n{_ROW_A}\n{_ROW_B.replace('/ 58', '/ 61')}\n")

    class _GC:
        def list_attachments(self, mid):
            return [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "id": "att-1", "contentType": "application/pdf",
                "name": "cs_master.PDF",
            }]

        def download_attachment(self, mid, aid):
            return drifted

    totals = vbi.ingest_message_attachments(
        tenant_db,
        _GC(),
        {"id": "AAMk-drift", "hasAttachments": True,
         "from": {"emailAddress": {"address": "carol.akes@supplier-domain.test"}}},
        ["supplier-domain.test"],
        llm_client=None,
    )

    assert totals["statement_unparseable"] == 1
    assert totals["unparseable"] == 0
    assert totals["statements"] == 0
    assert tenant_db.query(VendorStatement).count() == 0
