"""Vendor statement service — upload + email-ingest pipelines.

Manual upload (``upload_midwest_statement``):
    bytes -> sha256 -> dedup-check against tenant `documents` -> save to disk
    -> create Document row -> parse -> create VendorStatement + lines.

Email ingest (``ingest_midwest_statement``): same parse + persist, different
dedup posture. The manual path *raises* on a content-hash hit so the office
sees a 409 ("you already uploaded this"); the email path can't raise at a
human, so it resolves the same collisions into a returned duplicate result.
Both share ``_persist_parsed_statement`` so the two paths can never drift in
what they actually write.
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
# The only vendor with a statement parser today. Named here (not inlined) so
# the period-dedup lookup and the row it writes can't disagree on the string.
MIDWEST_VENDOR_NAME = "Midwest Wholesale Doors"


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
    document: Document | None
    created: bool
    # Why an existing statement was returned instead of a new one, if applicable.
    # 'content_hash' | 'statement_period'. Always None on the manual path, which
    # raises DuplicateDocumentError instead of returning a duplicate result.
    duplicate_reason: str | None = None


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


def find_statement_for_document(db: Session, document_id) -> VendorStatement | None:
    """The live statement already parsed out of this Document, if any."""
    return db.execute(
        select(VendorStatement)
        .where(VendorStatement.document_id == document_id)
        .where(VendorStatement.deleted_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()


def find_statement_by_period(
    db: Session,
    *,
    vendor_name: str,
    vendor_code: str | None,
    statement_date,
) -> VendorStatement | None:
    """Second dedup layer for the email path — the same statement period from
    the same vendor, regardless of bytes.

    Content-hash dedup alone is not enough here: the vendor re-sends the same
    month as a forward or a re-print, which is byte-different but is the SAME
    statement. Two rows for one period would double-count the balance in any
    later reconciliation, so the period is the identity.

    ``statement_date is None`` never matches — an unparsed date carries no
    identity, and silently collapsing every undated statement into one row
    would lose real documents. Those fall through and are recorded separately.

    Deliberately a query-level guard, not a DB unique index: these tables
    predate the migration chain (created via create_all, see migration 024), a
    unique index would need hand-applied DDL on every existing deployment, and
    it would hard-fail a legitimate same-date statement under a second customer
    code. Sequential-safety is what's needed and what this gives — the ingest
    processes one message at a time inside a single session, and the
    ``vendor_bills_ingested_at`` checkpoint plus the content-hash layer cover
    the re-delivery paths.
    """
    if statement_date is None:
        return None
    stmt = (
        select(VendorStatement)
        .where(VendorStatement.vendor_name == vendor_name)
        .where(VendorStatement.statement_date == statement_date)
        .where(VendorStatement.deleted_at.is_(None))
    )
    if vendor_code:
        stmt = stmt.where(VendorStatement.vendor_code == vendor_code)
    return db.execute(stmt.limit(1)).scalar_one_or_none()


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
    source: str = "upload",
) -> UploadResult:
    """Upload + parse a Midwest statement PDF (manual/office path).

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
    return _persist_parsed_statement(
        db,
        pdf_bytes=pdf_bytes,
        content_hash=content_hash,
        existing_doc=None,
        parsed=parsed,
        original_filename=original_filename,
        content_type=content_type,
        uploaded_by=uploaded_by,
        source=source,
    )


def ingest_midwest_statement(
    db: Session,
    *,
    pdf_bytes: bytes,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
    source: str = "email",
) -> UploadResult:
    """Email-path counterpart to ``upload_midwest_statement``.

    Same parser, same persistence — but a collision is a returned duplicate
    rather than a raised error, because there is no human on this path to show
    a 409 to. Three outcomes:

    - **Content hash hits a Document that already has a statement** → that
      statement, ``created=False``. The vendor re-sent identical bytes.
    - **Content hash hits a Document with NO statement** → reuse the Document
      (no byte re-store) and create the statement against it. This is the case
      the manual path can't express: the same PDF was already filed as a job
      attachment or pushed through the bill pipeline, and refusing here would
      mean the statement never lands at all.
    - **Period already recorded** (see ``find_statement_by_period``) → that
      statement, ``created=False``. A forward/re-print of a month we have.

    Raises ``MidwestParseError`` if the PDF isn't a parseable Midwest
    statement — the caller's signal to try the next rung.
    """
    if not pdf_bytes:
        raise MidwestParseError("empty file")

    # Parse FIRST, before any database work. On this path the parser is the
    # discriminator — the caller is walking a rung ladder and hands us every
    # PDF the bill parser rejected, most of which are not statements at all.
    # Parsing is in-memory and free, so answering "is this even ours?" before
    # opening a query keeps the rung honest: a scanned invoice on its way to
    # the LLM costs us nothing. (The invoice pipeline dedups before extracting
    # for the opposite reason — there, extraction can mean paid LLM tokens.)
    parsed = parse_midwest_statement(pdf_bytes)

    content_hash = compute_sha256(pdf_bytes)
    existing_doc = find_existing_document(db, content_hash)
    if existing_doc is not None:
        existing_statement = find_statement_for_document(db, existing_doc.id)
        if existing_statement is not None:
            return UploadResult(
                statement=existing_statement,
                document=existing_doc,
                created=False,
                duplicate_reason="content_hash",
            )

    period_dup = find_statement_by_period(
        db,
        vendor_name=MIDWEST_VENDOR_NAME,
        vendor_code=parsed.customer_code,
        statement_date=parsed.statement_date,
    )
    if period_dup is not None:
        return UploadResult(
            statement=period_dup,
            document=existing_doc,
            created=False,
            duplicate_reason="statement_period",
        )

    return _persist_parsed_statement(
        db,
        pdf_bytes=pdf_bytes,
        content_hash=content_hash,
        existing_doc=existing_doc,
        parsed=parsed,
        original_filename=original_filename,
        content_type=content_type,
        uploaded_by=uploaded_by,
        source=source,
    )


def _persist_parsed_statement(
    db: Session,
    *,
    pdf_bytes: bytes,
    content_hash: str,
    existing_doc: Document | None,
    parsed,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
    source: str,
) -> UploadResult:
    """Store the Document (unless reusing one) + the statement and its lines.
    Shared verbatim by the manual and email paths so what lands in the tables
    can never depend on which door the PDF came through."""
    if existing_doc is not None:
        document = existing_doc
    else:
        folder = _get_or_create_folder(db, VENDOR_STATEMENTS_FOLDER, uploaded_by)

        upload_root = _upload_dir()
        upload_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(original_filename).suffix or ".pdf"
        stored_filename = f"{uuid4()}{suffix.lower()}"
        output_path = upload_root / stored_filename
        with output_path.open("wb") as out:
            out.write(pdf_bytes)

        origin = "email" if source == "email" else "vendor statement upload"
        document = Document(
            filename=stored_filename,
            original_name=original_filename or stored_filename,
            file_size=len(pdf_bytes),
            content_type=content_type or "application/pdf",
            uploaded_by=uploaded_by or "",
            title=f"Midwest Statement {parsed.statement_date or ''}".strip(),
            description=f"Auto-imported via {origin} (parser={MIDWEST_PARSER_NAME})",
            folder_id=folder.id,
            content_hash=content_hash,
        )
        db.add(document)
        db.flush()

    statement = VendorStatement(
        vendor_name=MIDWEST_VENDOR_NAME,
        vendor_code=parsed.customer_code,
        statement_date=parsed.statement_date,
        document_id=document.id,
        parser_name=MIDWEST_PARSER_NAME,
        parser_version=MIDWEST_PARSER_VERSION,
        raw_total=parsed.raw_total,
        line_count=parsed.line_count,
        status="parsed",
        source=source,
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
