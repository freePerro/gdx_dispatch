"""Vendor invoice intake service — upload + dedup pipeline.

    bytes -> sha256 -> content-hash dedup (layer 1) -> parse
    -> resolve vendor -> (vendor, invoice#) dedup (layer 2)
    -> store Document + VendorInvoice + lines -> flag possible-dup (layer 3)

Mirrors the sibling ``vendor_statements`` service; reuses its content-hash
helpers so dedup semantics stay identical across both A/P intake paths.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Document, DocumentFolder
from gdx_dispatch.modules.vendor_invoices.llm_extract import (
    LLM_EXTRACTION_MODEL,
    LLMExtractionError,
    extract_invoice_via_llm,
)
from gdx_dispatch.modules.vendor_invoices.matching import (
    compute_vendor_key,
    find_duplicate_invoice,
    find_invoice_by_key,
    flag_possible_duplicate,
    resolve_vendor,
)
from gdx_dispatch.modules.vendor_invoices.models import (
    KIND_FREIGHT,
    KIND_ITEM,
    KIND_TAX,
    VendorInvoice,
    VendorInvoiceLine,
)
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    PARSER_NAME as MIDWEST_INVOICE_PARSER,
)
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    VENDOR_NAME as MIDWEST_VENDOR_NAME,
)
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    MidwestInvoiceParseError,
    ParsedInvoice,
    parse_midwest_invoice,
)
from gdx_dispatch.modules.vendor_statements.service import (
    compute_sha256,
    find_existing_document,
)

log = logging.getLogger(__name__)

VENDOR_BILLS_FOLDER = "Vendor Bills"
# Above this the printed Total and the extracted lines disagree structurally —
# the office fixes it in the manual queue rather than trusting the parse.
INVARIANT_TOLERANCE = Decimal("0.02")


class InvoiceParseError(MidwestInvoiceParseError):
    """Re-exported so callers import parse errors from the service."""


@dataclass
class InvoiceUploadResult:
    invoice: VendorInvoice
    document: Document | None
    created: bool
    # Why an existing record was returned instead of a new one, if applicable.
    duplicate_reason: str | None = None  # 'content_hash' | 'vendor_invoice_number'
    duplicate_of: VendorInvoice | None = None  # layer-3 advisory hint
    invariant_ok: bool = True


def _upload_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads/"))


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


def _invoice_for_document(db: Session, document_id) -> VendorInvoice | None:
    return db.execute(
        select(VendorInvoice)
        .where(VendorInvoice.document_id == document_id)
        .where(VendorInvoice.deleted_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()


def build_lines_from_parsed(parsed: ParsedInvoice) -> list[VendorInvoiceLine]:
    """Materialize item lines plus synthetic freight/tax lines so shipping and
    tax dollars are routable (never leak between payables and costing)."""
    lines: list[VendorInvoiceLine] = []
    for pl in parsed.lines:
        lines.append(
            VendorInvoiceLine(
                line_no=pl.line_no,
                kind=KIND_ITEM,
                item_label=pl.item_label,
                description=pl.description[:500],
                quantity=pl.quantity,
                unit_cost=pl.unit_price,
                line_total=pl.line_total,
            )
        )
    if parsed.shipping and parsed.shipping > 0:
        lines.append(
            VendorInvoiceLine(
                line_no=None,
                kind=KIND_FREIGHT,
                item_label="Shipping & Handling",
                description="Shipping & Handling",
                quantity=Decimal("1"),
                unit_cost=parsed.shipping,
                line_total=parsed.shipping,
            )
        )
    if parsed.tax and parsed.tax > 0:
        lines.append(
            VendorInvoiceLine(
                line_no=None,
                kind=KIND_TAX,
                item_label="Sales Tax",
                description="Sales Tax",
                quantity=Decimal("1"),
                unit_cost=parsed.tax,
                line_total=parsed.tax,
            )
        )
    return lines


def _layer1_content_hash(
    db: Session, content_hash: str
) -> tuple[Document | None, InvoiceUploadResult | None]:
    """Layer 1 — content hash. A hash hit on a Document that already has an
    invoice short-circuits (second element); a hash hit on a Document WITHOUT
    one (same PDF was earlier attached to a job) reuses the Document and
    proceeds. Runs BEFORE any extraction, so a re-seen PDF costs neither a
    parse nor an LLM call."""
    existing_doc = find_existing_document(db, content_hash)
    if existing_doc is not None:
        existing_inv = _invoice_for_document(db, existing_doc.id)
        if existing_inv is not None:
            return existing_doc, InvoiceUploadResult(
                invoice=existing_inv,
                document=existing_doc,
                created=False,
                duplicate_reason="content_hash",
            )
    return existing_doc, None


def upload_midwest_invoice(
    db: Session,
    *,
    pdf_bytes: bytes,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
    source: str = "upload",
) -> InvoiceUploadResult:
    """Upload + parse a Midwest retail-sale invoice PDF, applying dedup layers
    1 & 2 and flagging layer 3. Idempotent on both content hash and
    (vendor, invoice_number).

    Raises ``MidwestInvoiceParseError`` if the PDF isn't a parseable Midwest
    invoice (rung 2 — ``upload_invoice_via_llm`` — is the caller's fallback).
    """
    if not pdf_bytes:
        raise MidwestInvoiceParseError("empty file")

    content_hash = compute_sha256(pdf_bytes)
    existing_doc, early = _layer1_content_hash(db, content_hash)
    if early is not None:
        return early

    parsed = parse_midwest_invoice(pdf_bytes)
    return _persist_parsed_invoice(
        db,
        pdf_bytes=pdf_bytes,
        content_hash=content_hash,
        existing_doc=existing_doc,
        parsed=parsed,
        vendor_name_raw=MIDWEST_VENDOR_NAME,
        extraction_method="parser",
        extractor_label=MIDWEST_INVOICE_PARSER,
        original_filename=original_filename,
        content_type=content_type,
        uploaded_by=uploaded_by,
        source=source,
    )


def upload_invoice_via_llm(
    db: Session,
    *,
    pdf_bytes: bytes,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
    source: str = "email",
    llm_client,
) -> InvoiceUploadResult:
    """Rung 2: extract an arbitrary vendor's bill via the tenant's Anthropic
    model, then run the IDENTICAL dedup + persist pipeline as the parser path.
    The vendor name comes from the extraction (not a parser constant), so
    resolve_vendor/vendor_key handle any supplier.

    Raises ``LLMExtractionError`` when the model can't read the document as an
    invoice; lets transport/API errors propagate (callers retry those).
    """
    if not pdf_bytes:
        raise LLMExtractionError("empty file")

    content_hash = compute_sha256(pdf_bytes)
    existing_doc, early = _layer1_content_hash(db, content_hash)
    if early is not None:
        return early  # dedup BEFORE extraction — no token re-spend

    vendor_name_raw, parsed = extract_invoice_via_llm(llm_client, pdf_bytes)
    return _persist_parsed_invoice(
        db,
        pdf_bytes=pdf_bytes,
        content_hash=content_hash,
        existing_doc=existing_doc,
        parsed=parsed,
        vendor_name_raw=vendor_name_raw,
        extraction_method="llm",
        extractor_label=f"llm:{LLM_EXTRACTION_MODEL}",
        original_filename=original_filename,
        content_type=content_type,
        uploaded_by=uploaded_by,
        source=source,
    )


def _persist_parsed_invoice(
    db: Session,
    *,
    pdf_bytes: bytes,
    content_hash: str,
    existing_doc: Document | None,
    parsed: ParsedInvoice,
    vendor_name_raw: str,
    extraction_method: str,
    extractor_label: str,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
    source: str,
) -> InvoiceUploadResult:
    """Layers 2+3 and persistence, shared verbatim by the parser and LLM rungs
    so dedup and review semantics can never drift between them."""
    vendor = resolve_vendor(db, vendor_name_raw)
    vendor_id = vendor.id if vendor else None

    # Layer 2 — (vendor, invoice_number) uniqueness. Checked BEFORE storing any
    # new bytes so a re-print/re-scan with different bytes doesn't create a
    # second Document.
    dup = find_duplicate_invoice(
        db,
        vendor_id=vendor_id,
        vendor_name_raw=vendor_name_raw,
        invoice_number=parsed.invoice_number,
    )
    if dup is not None:
        return InvoiceUploadResult(
            invoice=dup,
            document=existing_doc,
            created=False,
            duplicate_reason="vendor_invoice_number",
            duplicate_of=dup,
        )

    # Persist the document (reuse an existing one on a content-hash-only hit).
    new_file_path: Path | None = None
    if existing_doc is not None:
        document = existing_doc
    else:
        upload_root = _upload_dir()
        upload_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(original_filename).suffix or ".pdf"
        stored_filename = f"{uuid4()}{suffix.lower()}"
        new_file_path = upload_root / stored_filename
        new_file_path.write_bytes(pdf_bytes)

        folder = _get_or_create_folder(db, VENDOR_BILLS_FOLDER, uploaded_by)
        document = Document(
            filename=stored_filename,
            original_name=original_filename or stored_filename,
            file_size=len(pdf_bytes),
            content_type=content_type or "application/pdf",
            uploaded_by=uploaded_by or "",
            title=f"{vendor_name_raw} Invoice {parsed.invoice_number}".strip(),
            description=f"Auto-imported vendor bill (extractor={extractor_label})",
            folder_id=folder.id,
            content_hash=content_hash,
        )
        db.add(document)
        db.flush()

    # Structural validation: the header must balance AND every line's
    # qty*unit_cost must equal its printed total. Header-only would miss a
    # quantity misread that keeps the line total (and thus the header) correct
    # but feeds a wrong number into inventory/billing — so we check both.
    invariant_disc = parsed.invariant_discrepancy()
    worst_line_disc = max(
        (ln.line_math_discrepancy() for ln in parsed.lines),
        default=Decimal("0"),
    )
    invariant_ok = invariant_disc <= INVARIANT_TOLERANCE and worst_line_disc <= INVARIANT_TOLERANCE

    note_parts: list[str] = []
    if extraction_method == "llm":
        # LLM output is data, not truth — flag it so the office verifies
        # against the PDF before confirming any line.
        note_parts.append(f"LLM_EXTRACTED ({extractor_label}): verify against the PDF")
    if not invariant_ok:
        note_parts.append(
            f"INVARIANT_MISMATCH: header off by {invariant_disc}, "
            f"worst line qty*unit vs total off by {worst_line_disc}"
        )

    invoice = VendorInvoice(
        vendor_id=vendor_id,
        vendor_key=compute_vendor_key(vendor_id, vendor_name_raw),
        vendor_name_raw=vendor_name_raw,
        invoice_number=parsed.invoice_number,
        invoice_date=parsed.invoice_date,
        po_reference=parsed.po_reference,
        terms=parsed.terms,
        due_date=parsed.due_date,
        subtotal=parsed.subtotal,
        tax=parsed.tax,
        shipping=parsed.shipping,
        total=parsed.total,
        document_id=document.id if document else None,
        source=source,
        extraction_method=extraction_method,
        uploaded_by=uploaded_by,
        notes="; ".join(note_parts) or None,
    )
    invoice.lines = build_lines_from_parsed(parsed)
    db.add(invoice)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent upload won the (vendor_key, invoice_number) unique index.
        # Roll back our aborted transaction (discards the Document ROW we just
        # created) and delete the PDF we wrote to disk before the flush (rollback
        # can't un-write it), so the concurrent loser leaves no orphan. Then
        # return the winner as the dedup result.
        db.rollback()
        if new_file_path is not None:
            new_file_path.unlink(missing_ok=True)
        winner = find_invoice_by_key(
            db,
            vendor_key=compute_vendor_key(vendor_id, vendor_name_raw),
            invoice_number=parsed.invoice_number,
        )
        if winner is not None:
            return InvoiceUploadResult(
                invoice=winner,
                document=None,
                created=False,
                duplicate_reason="vendor_invoice_number",
                duplicate_of=winner,
            )
        raise  # not a dedup collision we can resolve — surface it

    # Layer 3 — advisory possible-duplicate flag (never blocks).
    duplicate_of = flag_possible_duplicate(db, invoice)

    return InvoiceUploadResult(
        invoice=invoice,
        document=document,
        created=True,
        duplicate_of=duplicate_of,
        invariant_ok=invariant_ok,
    )
