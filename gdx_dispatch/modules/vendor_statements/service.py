"""Vendor statement service — slice 1.

Handles the upload pipeline:
    bytes -> sha256 -> dedup-check against tenant `documents` -> save to disk
    -> create Document row -> parse -> create VendorStatement + lines.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from json import dumps
from pathlib import Path
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Document, DocumentFolder
from gdx_dispatch.modules.vendor_statements.classifier import classify_line
from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine
from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    PARSER_NAME as MIDWEST_PARSER_NAME,
    PARSER_VERSION as MIDWEST_PARSER_VERSION,
    MidwestParseError,
    parse_midwest_statement,
)

log = logging.getLogger(__name__)

VENDOR_STATEMENTS_FOLDER = "Vendor Statements"


class DuplicateDocumentError(Exception):
    """Raised when a Document with the same content_hash already exists in the tenant."""

    def __init__(self, existing_document_id: str, original_name: str | None = None):
        self.existing_document_id = existing_document_id
        self.original_name = original_name
        super().__init__(
            f"Duplicate document (existing id={existing_document_id})"
        )


@dataclass
class UploadResult:
    statement: VendorStatement
    document: Document
    created: bool


def _upload_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads/"))


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_existing_document(db: Session, content_hash: str) -> Optional[Document]:
    """Return a non-deleted Document in the tenant matching the hash, if any."""
    stmt = (
        select(Document)
        .where(Document.content_hash == content_hash)
        .where(Document.deleted_at.is_(None))
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _get_or_create_folder(db: Session, name: str, created_by: str | None) -> DocumentFolder:
    folder = db.execute(
        select(DocumentFolder)
        .where(DocumentFolder.name == name)
        .where(DocumentFolder.deleted_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()
    if folder:
        return folder
    folder = DocumentFolder(name=name, created_by=created_by)
    db.add(folder)
    db.flush()
    return folder


def upload_midwest_statement(
    db: Session,
    *,
    pdf_bytes: bytes,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
) -> UploadResult:
    """Upload + parse a Midwest statement PDF.

    Raises:
        DuplicateDocumentError — if the same file (by sha256) already exists.
        MidwestParseError       — if the PDF can't be parsed.
    """
    if not pdf_bytes:
        raise MidwestParseError("empty file")

    content_hash = compute_sha256(pdf_bytes)

    existing = find_existing_document(db, content_hash)
    if existing is not None:
        raise DuplicateDocumentError(
            existing_document_id=str(existing.id),
            original_name=existing.original_name,
        )

    parsed = parse_midwest_statement(pdf_bytes)

    folder = _get_or_create_folder(db, VENDOR_STATEMENTS_FOLDER, uploaded_by)

    upload_root = _upload_dir()
    upload_root.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_filename).suffix or ".pdf"
    stored_filename = f"{uuid4()}{suffix.lower()}"
    output_path = upload_root / stored_filename
    with output_path.open("wb") as out:
        out.write(pdf_bytes)

    document = Document(
        filename=stored_filename,
        original_name=original_filename or stored_filename,
        file_size=len(pdf_bytes),
        content_type=content_type or "application/pdf",
        uploaded_by=uploaded_by or "",
        title=f"Midwest Statement {parsed.statement_date or ''}".strip(),
        description=f"Auto-imported via vendor statement upload (parser={MIDWEST_PARSER_NAME})",
        folder_id=folder.id,
        content_hash=content_hash,
    )
    db.add(document)
    db.flush()

    statement = VendorStatement(
        vendor_name="Midwest Wholesale Doors",
        vendor_code=parsed.customer_code,
        statement_date=parsed.statement_date,
        document_id=document.id,
        parser_name=MIDWEST_PARSER_NAME,
        parser_version=MIDWEST_PARSER_VERSION,
        raw_total=parsed.raw_total,
        line_count=parsed.line_count,
        status="parsed",
        uploaded_by=uploaded_by,
    )
    db.add(statement)
    db.flush()

    for parsed_line in parsed.lines:
        aging_breakdown = {
            "0-29": str(parsed_line.aging_0_29),
            "30-59": str(parsed_line.aging_30_59),
            "60-89": str(parsed_line.aging_60_89),
            "90-119": str(parsed_line.aging_90_119),
            "120+": str(parsed_line.aging_120_plus),
            "retainage": str(parsed_line.retainage),
        }
        line = VendorStatementLine(
            statement_id=statement.id,
            line_no=parsed_line.line_no,
            vendor_invoice_no=parsed_line.invoice_no,
            vendor_job_no=parsed_line.job_no,
            line_date=parsed_line.line_date,
            amount=parsed_line.amount,
            balance=parsed_line.balance,
            description=parsed_line.description,
            po_ref=parsed_line.po_ref,
            aging_bucket=parsed_line.aging_bucket,
            classification=classify_line(parsed_line.description),
            raw_text=parsed_line.raw_text,
            raw_aging_json=dumps(aging_breakdown),
        )
        db.add(line)

    db.flush()
    return UploadResult(statement=statement, document=document, created=True)
