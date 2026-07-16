"""Vendor invoice upload service — end-to-end pipeline test.

Runs the full bytes -> parse -> dedup -> persist pipeline against a REAL sample
invoice PDF, only when ``VENDOR_INVOICE_SAMPLE_PDF`` points at one (skips in CI
so no invoice data is needed or committed). Assertions are structural — no
hard-coded private values from any specific bill.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from gdx_dispatch.models.tenant_models import Document
from gdx_dispatch.modules.vendor_invoices.models import (
    KIND_FREIGHT,
    KIND_ITEM,
    VendorInvoice,
    VendorInvoiceLine,
)
from gdx_dispatch.modules.vendor_invoices.service import upload_midwest_invoice


def _sample() -> bytes:
    path = os.getenv("VENDOR_INVOICE_SAMPLE_PDF", "").strip()
    if not path or not Path(path).exists():
        pytest.skip("set VENDOR_INVOICE_SAMPLE_PDF to a real invoice PDF to run this")
    return Path(path).read_bytes()


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    yield


def test_upload_happy_path(tenant_db):
    pdf = _sample()
    result = upload_midwest_invoice(
        tenant_db,
        pdf_bytes=pdf,
        original_filename="invoice.pdf",
        content_type="application/pdf",
        uploaded_by="user-test",
    )
    tenant_db.commit()

    assert result.created is True
    assert result.invariant_ok is True
    inv = result.invoice
    assert inv.invoice_number
    assert inv.total > 0
    assert inv.vendor_name_raw == "Midwest Wholesale Doors"
    assert inv.document_id is not None

    # Item lines materialized; subtotal is the sum of item lines only.
    item_lines = [ln for ln in inv.lines if ln.kind == KIND_ITEM]
    assert len(item_lines) >= 1
    assert inv.subtotal == sum((ln.line_total for ln in item_lines), Decimal("0.00"))

    # If the bill carries shipping, it becomes a routable freight line (not
    # folded into an item, not lost).
    if inv.shipping > 0:
        freight = [ln for ln in inv.lines if ln.kind == KIND_FREIGHT]
        assert len(freight) == 1
        assert freight[0].line_total == inv.shipping

    assert tenant_db.query(Document).count() == 1
    assert tenant_db.query(VendorInvoice).count() == 1


def test_upload_content_hash_dedup(tenant_db):
    pdf = _sample()
    first = upload_midwest_invoice(
        tenant_db, pdf_bytes=pdf, original_filename="a.pdf",
        content_type="application/pdf", uploaded_by="u",
    )
    tenant_db.commit()

    second = upload_midwest_invoice(
        tenant_db, pdf_bytes=pdf, original_filename="b.pdf",
        content_type="application/pdf", uploaded_by="u",
    )
    tenant_db.commit()

    assert second.created is False
    assert second.duplicate_reason == "content_hash"
    assert second.invoice.id == first.invoice.id
    # No second Document or VendorInvoice
    assert tenant_db.query(Document).count() == 1
    assert tenant_db.query(VendorInvoice).count() == 1


def test_upload_lines_persist(tenant_db):
    pdf = _sample()
    result = upload_midwest_invoice(
        tenant_db, pdf_bytes=pdf, original_filename="a.pdf",
        content_type="application/pdf", uploaded_by="u",
    )
    tenant_db.commit()
    persisted = (
        tenant_db.query(VendorInvoiceLine)
        .filter(VendorInvoiceLine.vendor_invoice_id == result.invoice.id)
        .count()
    )
    assert persisted == len(result.invoice.lines)
    assert persisted >= 1
