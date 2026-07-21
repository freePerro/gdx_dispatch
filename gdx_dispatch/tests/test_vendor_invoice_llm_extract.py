"""Rung 2 — LLM extraction for vendor-bill PDFs (Phase 2, D4).

Two layers:
- ``extract_invoice_via_llm``: pure mapping tests against a fake Anthropic
  client (request wiring, Decimal/date coercion, validation rejects).
- ``upload_invoice_via_llm``: the service pipeline against a real tenant DB —
  proves the LLM path shares the parser path's dedup + review semantics, and
  that content-hash dedup fires BEFORE extraction (no token re-spend).
"""
from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from gdx_dispatch.modules.vendor_invoices import llm_extract, service
from gdx_dispatch.modules.vendor_invoices.llm_extract import (
    LLM_EXTRACTION_MODEL,
    MAX_LLM_PDF_BYTES,
    LLMExtractionError,
    extract_invoice_via_llm,
)
from gdx_dispatch.modules.vendor_invoices.models import (
    KIND_FREIGHT,
    KIND_ITEM,
    KIND_TAX,
)
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    ParsedInvoice,
    ParsedInvoiceLine,
)
from gdx_dispatch.modules.vendor_invoices.service import upload_invoice_via_llm


class _FakeAnthropic:
    def __init__(self, tool_input=None, content=None):
        self.calls = []
        if content is None:
            content = [SimpleNamespace(type="tool_use", input=tool_input)]
        self._content = content
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self._content)


def _good_input(**over):
    d = {
        "vendor_name": "Acme Door Supply",
        "invoice_number": "INV-1001",
        "invoice_date": "2026-07-01",
        "po_reference": "PO-77",
        "terms": "Net 30",
        "due_date": "2026-07-31",
        "tax": "12.34",
        "shipping": "25.00",
        "total": "237.34",
        "lines": [
            {"item_label": "DOOR-9x7", "description": "9x7 insulated door",
             "quantity": "2", "unit_price": "100.00", "line_total": "200.00"},
        ],
    }
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# extract_invoice_via_llm — mapping + wiring
# --------------------------------------------------------------------------- #
def test_extract_happy_path_maps_onto_parsed_invoice():
    client = _FakeAnthropic(_good_input())
    vendor, parsed = extract_invoice_via_llm(client, b"%PDF-1.4 x")

    assert vendor == "Acme Door Supply"
    assert parsed.invoice_number == "INV-1001"
    assert parsed.invoice_date == date(2026, 7, 1)
    assert parsed.due_date == date(2026, 7, 31)
    assert parsed.terms == "Net 30"
    assert parsed.po_reference == "PO-77"
    assert parsed.tax == Decimal("12.34")
    assert parsed.shipping == Decimal("25.00")
    assert parsed.total == Decimal("237.34")
    assert parsed.subtotal == Decimal("200.00")
    assert parsed.invariant_discrepancy() == Decimal("0.00")
    (ln,) = parsed.lines
    assert ln.quantity == Decimal("2.00")
    assert ln.unit_price == Decimal("100.00")
    assert ln.line_math_discrepancy() == Decimal("0.00")


def test_extract_request_wiring_forces_tool_and_embeds_pdf():
    pdf = b"%PDF-1.4 real bytes"
    client = _FakeAnthropic(_good_input())
    extract_invoice_via_llm(client, pdf)

    (kwargs,) = client.calls
    assert kwargs["model"] == LLM_EXTRACTION_MODEL
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_invoice"}
    doc_block = kwargs["messages"][0]["content"][0]
    assert doc_block["type"] == "document"
    assert doc_block["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(doc_block["source"]["data"]) == pdf


def test_extract_accepts_float_amounts_and_quantizes():
    client = _FakeAnthropic(_good_input(total=237.34, tax=12.34, shipping=25.0))
    _, parsed = extract_invoice_via_llm(client, b"%PDF")
    assert parsed.total == Decimal("237.34")
    assert parsed.tax == Decimal("12.34")


def test_extract_derives_unit_price_and_defaults_quantity():
    lines = [
        {"description": "three hinges", "quantity": 3, "line_total": "30.00"},
        {"description": "service charge", "line_total": "45.00"},
    ]
    client = _FakeAnthropic(_good_input(lines=lines, tax=None, shipping=None, total="75.00"))
    _, parsed = extract_invoice_via_llm(client, b"%PDF")
    l1, l2 = parsed.lines
    assert l1.unit_price == Decimal("10.00")            # derived
    assert l2.quantity == Decimal("1.00")               # defaulted
    assert l2.unit_price == Decimal("45.00")
    assert parsed.invariant_discrepancy() == Decimal("0.00")


def test_extract_rejects_non_invoice_marker():
    client = _FakeAnthropic(_good_input(invoice_number=""))
    with pytest.raises(LLMExtractionError, match="not an invoice"):
        extract_invoice_via_llm(client, b"%PDF")


@pytest.mark.parametrize("over", [
    {"vendor_name": "  "},
    {"total": "0.00"},
    {"total": None},
    {"lines": []},
    {"total": "not-money"},
    {"lines": [{"description": "", "line_total": "5.00"}]},
])
def test_extract_rejects_unusable_output(over):
    client = _FakeAnthropic(_good_input(**over))
    with pytest.raises(LLMExtractionError):
        extract_invoice_via_llm(client, b"%PDF")


def test_extract_rejects_missing_tool_use_block():
    client = _FakeAnthropic(content=[SimpleNamespace(type="text", text="I cannot")])
    with pytest.raises(LLMExtractionError, match="no structured extraction"):
        extract_invoice_via_llm(client, b"%PDF")


def test_extract_refuses_oversized_pdf_without_spending_tokens():
    client = _FakeAnthropic(_good_input())
    with pytest.raises(LLMExtractionError, match="too large"):
        extract_invoice_via_llm(client, b"x" * (MAX_LLM_PDF_BYTES + 1))
    assert client.calls == []


def test_extract_empty_file_rejected():
    client = _FakeAnthropic(_good_input())
    with pytest.raises(LLMExtractionError, match="empty"):
        extract_invoice_via_llm(client, b"")
    assert client.calls == []


# --------------------------------------------------------------------------- #
# upload_invoice_via_llm — service pipeline on a real tenant DB
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    yield


def _parsed(number="INV-2001", total="237.34"):
    return ParsedInvoice(
        invoice_number=number,
        invoice_date=date(2026, 7, 1),
        po_reference=None,
        terms="Net 30",
        net_days=None,
        due_date=date(2026, 7, 31),
        tax=Decimal("12.34"),
        shipping=Decimal("25.00"),
        total=Decimal(total),
        credits_pending=Decimal("0.00"),
        amount_due=None,
        lines=[ParsedInvoiceLine(
            line_no=1, item_label="DOOR-9x7", description="9x7 insulated door",
            quantity=Decimal("2.00"), package=None,
            unit_price=Decimal("100.00"), line_total=Decimal("200.00"),
        )],
    )


def _patch_extract(monkeypatch, vendor="Acme Door Supply", parsed=None):
    calls = []

    def fake(client, pdf_bytes):
        calls.append(pdf_bytes)
        return vendor, (parsed or _parsed())

    monkeypatch.setattr(service, "extract_invoice_via_llm", fake)
    return calls


def test_llm_upload_creates_review_queue_bill(tenant_db, monkeypatch):
    _patch_extract(monkeypatch)
    result = upload_invoice_via_llm(
        tenant_db, pdf_bytes=b"%PDF-A", original_filename="bill.pdf",
        content_type="application/pdf", uploaded_by="outlook", llm_client=object(),
    )
    tenant_db.flush()

    assert result.created is True
    inv = result.invoice
    assert inv.extraction_method == "llm"
    assert inv.vendor_name_raw == "Acme Door Supply"
    assert inv.vendor_key  # unresolved vendor still gets a dedup key
    assert "LLM_EXTRACTED" in (inv.notes or "")
    assert result.invariant_ok is True
    kinds = sorted(ln.kind for ln in inv.lines)
    assert kinds == sorted([KIND_ITEM, KIND_FREIGHT, KIND_TAX])
    assert result.document is not None
    assert "extractor=llm:" in (result.document.description or "")


def test_llm_upload_content_hash_dedup_skips_extraction(tenant_db, monkeypatch):
    calls = _patch_extract(monkeypatch)
    first = upload_invoice_via_llm(
        tenant_db, pdf_bytes=b"%PDF-B", original_filename="bill.pdf",
        content_type="application/pdf", uploaded_by="outlook", llm_client=object(),
    )
    tenant_db.flush()
    again = upload_invoice_via_llm(
        tenant_db, pdf_bytes=b"%PDF-B", original_filename="bill.pdf",
        content_type="application/pdf", uploaded_by="outlook", llm_client=object(),
    )
    assert first.created is True
    assert again.created is False
    assert again.duplicate_reason == "content_hash"
    assert len(calls) == 1  # the re-seen PDF never re-spent tokens


def test_llm_upload_vendor_number_dedup_on_different_bytes(tenant_db, monkeypatch):
    calls = _patch_extract(monkeypatch)
    first = upload_invoice_via_llm(
        tenant_db, pdf_bytes=b"%PDF-C1", original_filename="bill.pdf",
        content_type="application/pdf", uploaded_by="outlook", llm_client=object(),
    )
    tenant_db.flush()
    rescan = upload_invoice_via_llm(
        tenant_db, pdf_bytes=b"%PDF-C2-different-bytes", original_filename="bill2.pdf",
        content_type="application/pdf", uploaded_by="outlook", llm_client=object(),
    )
    assert first.created is True
    assert rescan.created is False
    assert rescan.duplicate_reason == "vendor_invoice_number"
    assert len(calls) == 2  # different bytes → extracted, then layer-2 caught it


def test_llm_upload_invariant_mismatch_flags_review(tenant_db, monkeypatch):
    _patch_extract(monkeypatch, parsed=_parsed(number="INV-2002", total="999.99"))
    result = upload_invoice_via_llm(
        tenant_db, pdf_bytes=b"%PDF-D", original_filename="bill.pdf",
        content_type="application/pdf", uploaded_by="outlook", llm_client=object(),
    )
    assert result.created is True
    assert result.invariant_ok is False
    notes = result.invoice.notes or ""
    assert "LLM_EXTRACTED" in notes
    assert "INVARIANT_MISMATCH" in notes


def test_llm_upload_empty_file_rejected(tenant_db):
    with pytest.raises(LLMExtractionError):
        upload_invoice_via_llm(
            tenant_db, pdf_bytes=b"", original_filename="x.pdf",
            content_type="application/pdf", uploaded_by="u", llm_client=object(),
        )


def test_llm_extract_module_reexported_by_service():
    # Ingest imports these through the service facade — keep it working.
    assert service.LLMExtractionError is llm_extract.LLMExtractionError
    assert service.extract_invoice_via_llm is not None


def test_extract_rejects_non_invoice_document_types():
    for doc_type in ("statement", "quote", "order_acknowledgment", "packing_slip"):
        client = _FakeAnthropic(_good_input(document_type=doc_type))
        with pytest.raises(LLMExtractionError, match=f"is a {doc_type}"):
            extract_invoice_via_llm(client, b"%PDF")


def test_extract_accepts_explicit_invoice_type_and_missing_type():
    # Explicit "invoice" and (leniently) an absent classification both pass —
    # the empty-invoice_number rule still backstops the missing case.
    for inp in (_good_input(document_type="invoice"), _good_input()):
        client = _FakeAnthropic(inp)
        _vendor, parsed = extract_invoice_via_llm(client, b"%PDF")
        assert parsed.invoice_number == "INV-1001"


def test_extract_rejects_truncated_tool_call():
    client = _FakeAnthropic(_good_input())
    # Simulate a max_tokens-truncated response.
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input=_good_input())],
        stop_reason="max_tokens",
    )
    client.messages = SimpleNamespace(create=lambda **kw: msg)
    with pytest.raises(LLMExtractionError, match="truncated"):
        extract_invoice_via_llm(client, b"%PDF")
