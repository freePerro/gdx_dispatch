"""Vendor order intake — store a parsed order confirmation, and thread it.

Mirrors the ``vendor_statements`` service: same content-hash dedup helpers, same
Document handling, same "collisions return a duplicate rather than raise"
posture on the email path, because there is no human on it to show a 409 to.

Dedup is on the supplier's ORDER NUMBER, not on bytes. They re-send the same
confirmation as forwards and re-prints; the order number is the thing that
identifies the order. A revised confirmation for an order already on file is
therefore reported as a duplicate and NOT applied — v1 records the order as
first seen. That is deliberate: silently overwriting quantities and prices from
a later PDF would rewrite committed spend with no audit trail, and a revision
is rare enough to be worth a human looking at it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Document, DocumentFolder
from gdx_dispatch.modules.vendor_orders.models import VendorOrder, VendorOrderLine
from gdx_dispatch.modules.vendor_orders.parsers.midwest_order import (
    PARSER_NAME as MIDWEST_ORDER_PARSER,
)
from gdx_dispatch.modules.vendor_orders.parsers.midwest_order import (
    MidwestOrderParseError,
    parse_midwest_order,
)
from gdx_dispatch.modules.vendor_statements.parsers.midwest import VENDOR_LETTERHEAD
from gdx_dispatch.modules.vendor_statements.service import (
    compute_sha256,
    find_existing_document,
)

log = logging.getLogger(__name__)

VENDOR_ORDERS_FOLDER = "Vendor Orders"


@dataclass
class OrderUploadResult:
    order: VendorOrder
    document: Document | None
    created: bool
    duplicate_reason: str | None = None  # 'order_number' | 'content_hash'


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


def find_order_by_number(db: Session, *, vendor_name: str, order_number: str) -> VendorOrder | None:
    """The live order with this supplier order number, if any."""
    return db.execute(
        select(VendorOrder)
        .where(VendorOrder.vendor_name == vendor_name)
        .where(VendorOrder.order_number == order_number)
        .where(VendorOrder.deleted_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()


def ingest_midwest_order(
    db: Session,
    *,
    pdf_bytes: bytes,
    original_filename: str,
    content_type: str | None,
    uploaded_by: str | None,
    source: str = "email",
) -> OrderUploadResult:
    """Parse + store one order confirmation.

    Parses BEFORE touching the database: the caller hands us every PDF the
    earlier parsers rejected, most of which are not order confirmations, and
    answering "is this even mine?" in memory costs nothing.

    Raises ``MidwestOrderParseError`` when it isn't one — the caller's signal to
    keep climbing. A structural failure (``MidwestOrderStructureError``, a
    subclass) propagates too, and callers are expected to treat it loudly rather
    than as a routine miss.
    """
    if not pdf_bytes:
        raise MidwestOrderParseError("empty file")

    parsed = parse_midwest_order(pdf_bytes)

    existing = find_order_by_number(
        db, vendor_name=VENDOR_LETTERHEAD, order_number=parsed.order_number
    )
    if existing is not None:
        return OrderUploadResult(
            order=existing, document=None, created=False, duplicate_reason="order_number"
        )

    content_hash = compute_sha256(pdf_bytes)
    existing_doc = find_existing_document(db, content_hash)

    if existing_doc is not None:
        document = existing_doc
    else:
        folder = _get_or_create_folder(db, VENDOR_ORDERS_FOLDER, uploaded_by)
        upload_root = _upload_dir()
        upload_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(original_filename).suffix or ".pdf"
        stored_filename = f"{uuid4()}{suffix.lower()}"
        (upload_root / stored_filename).write_bytes(pdf_bytes)
        document = Document(
            filename=stored_filename,
            original_name=original_filename or stored_filename,
            file_size=len(pdf_bytes),
            content_type=content_type or "application/pdf",
            uploaded_by=uploaded_by or "",
            title=f"Order {parsed.order_number}",
            description=f"Auto-imported order confirmation (parser={MIDWEST_ORDER_PARSER})",
            folder_id=folder.id,
            content_hash=content_hash,
        )
        db.add(document)
        db.flush()

    order = VendorOrder(
        vendor_name=VENDOR_LETTERHEAD,
        vendor_code=parsed.customer_code,
        order_number=parsed.order_number,
        order_date=parsed.order_date,
        salesperson=parsed.salesperson,
        ship_to=(parsed.ship_to or None),
        terms=(parsed.terms or None),
        customer_po=(parsed.customer_po or None),
        lot_no=parsed.lot_no,
        called_in_by=parsed.called_in_by,
        estimated_subtotal=parsed.estimated_subtotal,
        estimated_shipping=parsed.estimated_shipping,
        estimated_tax=parsed.estimated_tax,
        estimated_total=parsed.estimated_total,
        document_id=document.id if document else None,
        parser_name=MIDWEST_ORDER_PARSER,
        parser_version=1,
        line_count=parsed.line_count,
        source=source,
        uploaded_by=uploaded_by,
    )
    order.lines = [
        VendorOrderLine(
            line_no=pl.line_no,
            description=(pl.description or "")[:500] or None,
            notes=pl.notes,
            quantity=pl.quantity,
            unit=pl.unit,
            unit_cost=pl.unit_cost,
            line_total=pl.line_total,
        )
        for pl in parsed.lines
    ]
    db.add(order)
    db.flush()
    return OrderUploadResult(order=order, document=document, created=True)
