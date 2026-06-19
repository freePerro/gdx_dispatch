"""Sprint vendor-statement-recon slice 1 — service-level upload + dedup test.

Tests the `upload_midwest_statement` service function directly against an
isolated tenant DB. Bypasses FastAPI to keep the test focused on:
  - happy path: upload creates Document + VendorStatement + lines
  - dedup: re-uploading the same bytes raises DuplicateDocumentError
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gdx_dispatch.models.tenant_models import Document
from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine
from gdx_dispatch.modules.vendor_statements.service import (
    DuplicateDocumentError,
    upload_midwest_statement,
)


SAMPLE_PDF = Path("/path/to/sample-files/cs_master (41).PDF")


def _bytes_or_skip() -> bytes:
    if not SAMPLE_PDF.exists():
        pytest.skip(f"sample PDF not present at {SAMPLE_PDF}")
    return SAMPLE_PDF.read_bytes()


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    yield


def test_upload_happy_path(tenant_db):
    pdf = _bytes_or_skip()
    result = upload_midwest_statement(
        tenant_db,
        pdf_bytes=pdf,
        original_filename="cs_master (41).PDF",
        content_type="application/pdf",
        uploaded_by="user-test",
    )
    tenant_db.commit()

    assert result.created is True
    assert result.statement.line_count == 27
    assert result.statement.vendor_name == "Midwest Wholesale Doors"
    assert result.statement.parser_name == "midwest_v1"
    assert result.document.content_hash is not None
    assert len(result.document.content_hash) == 64

    # File on disk
    upload_dir = os.environ["UPLOAD_DIR"]
    assert any(Path(upload_dir).iterdir()), "expected file written under UPLOAD_DIR"

    # Persisted rows
    assert tenant_db.query(Document).count() == 1
    assert tenant_db.query(VendorStatement).count() == 1
    assert tenant_db.query(VendorStatementLine).count() == 27


def test_upload_duplicate_rejected(tenant_db):
    pdf = _bytes_or_skip()
    upload_midwest_statement(
        tenant_db,
        pdf_bytes=pdf,
        original_filename="first.pdf",
        content_type="application/pdf",
        uploaded_by="user-test",
    )
    tenant_db.commit()

    with pytest.raises(DuplicateDocumentError) as exc:
        upload_midwest_statement(
            tenant_db,
            pdf_bytes=pdf,
            original_filename="second-same-content.pdf",
            content_type="application/pdf",
            uploaded_by="user-test",
        )
    # Second upload didn't add a new Document or Statement
    assert tenant_db.query(Document).count() == 1
    assert tenant_db.query(VendorStatement).count() == 1
    assert exc.value.existing_document_id


def test_upload_populates_classification(tenant_db):
    """Slice 2: every persisted line gets a classification (job/inventory/unknown)."""
    pdf = _bytes_or_skip()
    result = upload_midwest_statement(
        tenant_db,
        pdf_bytes=pdf,
        original_filename="cs_master (41).PDF",
        content_type="application/pdf",
        uploaded_by="user-test",
    )
    tenant_db.commit()

    lines = (
        tenant_db.query(VendorStatementLine)
        .filter(VendorStatementLine.statement_id == result.statement.id)
        .all()
    )
    by_desc = {ln.description: ln.classification for ln in lines}
    assert by_desc["Stock"] == "inventory"
    assert by_desc["Op's 1.16.26"] == "inventory"
    assert by_desc["406x6x32"] == "inventory"
    assert by_desc["LYNN & JIM LIEPOLD"] == "job"
    assert by_desc["TREVOR JOHNSON"] == "job"
    assert by_desc["RUSS BISCHOFF"] == "job"
    assert by_desc["Add-ons"] == "unknown"
    assert by_desc["Wilke"] == "unknown"
    # Every line classified
    assert all(ln.classification in {"job", "inventory", "unknown"} for ln in lines)


def test_upload_after_soft_delete_allowed(tenant_db):
    """If the prior document was soft-deleted, the same bytes can be re-uploaded."""
    pdf = _bytes_or_skip()
    first = upload_midwest_statement(
        tenant_db,
        pdf_bytes=pdf,
        original_filename="first.pdf",
        content_type="application/pdf",
        uploaded_by="user-test",
    )
    tenant_db.commit()

    from datetime import datetime, timezone
    first.document.deleted_at = datetime.now(timezone.utc)
    tenant_db.commit()

    second = upload_midwest_statement(
        tenant_db,
        pdf_bytes=pdf,
        original_filename="second.pdf",
        content_type="application/pdf",
        uploaded_by="user-test",
    )
    tenant_db.commit()
    assert second.document.id != first.document.id
