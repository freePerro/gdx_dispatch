"""Vendor invoice (A/P bill) intake API — Phase 1.

    POST /api/vendor-invoices/upload            multipart PDF
    GET  /api/vendor-invoices                   review queue (list)
    GET  /api/vendor-invoices/payables          open bills + due dates
    GET  /api/vendor-invoices/{id}              detail + job suggestions + flags
    PATCH/api/vendor-invoices/{id}              set matched job / status
    POST /api/vendor-invoices/{id}/lines/{lid}/confirm   route a line + apply effects

Design: docs/design/vendor-invoice-intake-plan.md (Phase 1).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.modules.vendor_invoices.confirm import (
    ConfirmError,
    confirm_line,
    maybe_mark_reviewed,
)
from gdx_dispatch.modules.vendor_invoices.matching import suggest_job_matches
from gdx_dispatch.modules.vendor_invoices.models import (
    STATUS_OPEN,
    VALID_STATUSES,
    VendorInvoice,
)
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import MidwestInvoiceParseError
from gdx_dispatch.modules.vendor_invoices.service import upload_midwest_invoice

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vendor-invoices", tags=["vendor-invoices"])

_SUPPORTED_VENDORS = {"midwest"}


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int | None
    kind: str
    item_label: str | None
    description: str
    quantity: Decimal
    unit_cost: Decimal
    line_total: Decimal
    disposition: str
    status: str
    skip_reason: str | None
    job_id: UUID | None
    inventory_item_id: UUID | None
    expense_id: UUID | None
    job_part_needed_id: str | None


class InvoiceSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vendor_id: UUID | None
    vendor_name_raw: str
    invoice_number: str
    invoice_date: date | None
    po_reference: str | None
    terms: str | None
    due_date: date | None
    subtotal: Decimal
    tax: Decimal
    shipping: Decimal
    total: Decimal
    status: str
    matched_job_id: UUID | None
    document_id: UUID | None
    source: str
    extraction_method: str
    possible_duplicate_of_id: UUID | None
    reviewed_at: datetime | None
    notes: str | None
    created_at: datetime


class JobSuggestionOut(BaseModel):
    job_id: str
    score: float
    reason: str
    job_title: str | None = None
    customer_name: str | None = None
    lifecycle_stage: str | None = None


class InvoiceDetailOut(InvoiceSummaryOut):
    lines: list[LineOut] = []
    suggestions: list[JobSuggestionOut] = []
    invariant_ok: bool = True


class UploadResultOut(BaseModel):
    created: bool
    duplicate_reason: str | None = None
    invariant_ok: bool = True
    invoice: InvoiceDetailOut


class InvoicePatch(BaseModel):
    matched_job_id: UUID | None = None
    status: str | None = None


class ConfirmLineIn(BaseModel):
    disposition: str
    job_id: UUID | None = None
    inventory_item_id: UUID | None = None
    skip_reason: str | None = None
    update_catalog_cost: bool = False


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def _detail(db: Session, invoice: VendorInvoice, *, with_suggestions: bool = True) -> InvoiceDetailOut:
    summary = InvoiceSummaryOut.model_validate(invoice)
    lines = sorted(invoice.lines, key=lambda ln: (ln.line_no is None, ln.line_no or 0))
    suggestions: list[JobSuggestionOut] = []
    if with_suggestions and invoice.matched_job_id is None:
        suggestions = [
            JobSuggestionOut(**s.__dict__) for s in suggest_job_matches(db, invoice)
        ]
    return InvoiceDetailOut(
        **summary.model_dump(),
        lines=[LineOut.model_validate(ln) for ln in lines],
        suggestions=suggestions,
        invariant_ok=not (invoice.notes or "").startswith("INVARIANT_MISMATCH"),
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post(
    "/upload",
    response_model=UploadResultOut,
    dependencies=[Depends(require_permission("vendor_invoices.write"))],
)
async def upload_invoice(
    request: Request,
    file: UploadFile = File(...),
    vendor: str = Form(default="midwest"),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadResultOut:
    vendor_key = (vendor or "").strip().lower()
    if vendor_key not in _SUPPORTED_VENDORS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported vendor '{vendor}'. supported: {sorted(_SUPPORTED_VENDORS)}",
        )

    pdf_bytes = await file.read()
    try:
        result = upload_midwest_invoice(
            db,
            pdf_bytes=pdf_bytes,
            original_filename=file.filename or "midwest-invoice.pdf",
            content_type=file.content_type,
            uploaded_by=_actor(user),
        )
    except MidwestInvoiceParseError as exc:
        raise HTTPException(status_code=422, detail=f"could not parse invoice: {exc}") from exc

    db.commit()
    db.refresh(result.invoice)

    if result.created:
        log_audit_event_sync(
            db=db,
            tenant_id=_tid(request),
            user_id=_actor(user),
            action="vendor_invoice_uploaded",
            entity_type="vendor_invoice",
            entity_id=str(result.invoice.id),
            details={
                "vendor": vendor_key,
                "invoice_number": result.invoice.invoice_number,
                "total": str(result.invoice.total),
                "invariant_ok": result.invariant_ok,
                "possible_duplicate_of": (
                    str(result.duplicate_of.id) if result.duplicate_of else None
                ),
            },
        )
        db.commit()

    return UploadResultOut(
        created=result.created,
        duplicate_reason=result.duplicate_reason,
        invariant_ok=result.invariant_ok,
        invoice=_detail(db, result.invoice),
    )


@router.get(
    "",
    response_model=list[InvoiceSummaryOut],
    dependencies=[Depends(require_permission("vendor_invoices.read"))],
)
async def list_invoices(
    status: str | None = None,
    needs_review: bool = False,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InvoiceSummaryOut]:
    stmt = select(VendorInvoice).where(VendorInvoice.deleted_at.is_(None))
    if status:
        stmt = stmt.where(VendorInvoice.status == status)
    if needs_review:
        stmt = stmt.where(VendorInvoice.reviewed_at.is_(None))
    stmt = stmt.order_by(VendorInvoice.created_at.desc())
    rows = db.execute(stmt).scalars().all()
    return [InvoiceSummaryOut.model_validate(r) for r in rows]


@router.get(
    "/payables",
    response_model=list[InvoiceSummaryOut],
    dependencies=[Depends(require_permission("vendor_invoices.read"))],
)
async def list_payables(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InvoiceSummaryOut]:
    """Open (unpaid) bills, soonest due first — the cash-out picture."""
    stmt = (
        select(VendorInvoice)
        .where(VendorInvoice.deleted_at.is_(None))
        .where(VendorInvoice.status == STATUS_OPEN)
        .order_by(VendorInvoice.due_date.is_(None), VendorInvoice.due_date.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [InvoiceSummaryOut.model_validate(r) for r in rows]


@router.get(
    "/{invoice_id}",
    response_model=InvoiceDetailOut,
    dependencies=[Depends(require_permission("vendor_invoices.read"))],
)
async def get_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvoiceDetailOut:
    invoice = _load(db, invoice_id)
    return _detail(db, invoice)


@router.patch(
    "/{invoice_id}",
    response_model=InvoiceDetailOut,
    dependencies=[Depends(require_permission("vendor_invoices.write"))],
)
async def patch_invoice(
    invoice_id: UUID,
    payload: InvoicePatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvoiceDetailOut:
    invoice = _load(db, invoice_id)
    if payload.status is not None:
        if payload.status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {sorted(VALID_STATUSES)}")
        invoice.status = payload.status
    if payload.matched_job_id is not None:
        invoice.matched_job_id = payload.matched_job_id
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=_tid(request),
        user_id=_actor(user),
        action="vendor_invoice_updated",
        entity_type="vendor_invoice",
        entity_id=str(invoice.id),
        details={"status": invoice.status, "matched_job_id": str(invoice.matched_job_id or "")},
    )
    db.commit()
    return _detail(db, invoice)


@router.post(
    "/{invoice_id}/lines/{line_id}/confirm",
    response_model=InvoiceDetailOut,
    dependencies=[Depends(require_permission("vendor_invoices.write"))],
)
async def confirm_invoice_line(
    invoice_id: UUID,
    line_id: UUID,
    payload: ConfirmLineIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvoiceDetailOut:
    invoice = _load(db, invoice_id)
    line = next((ln for ln in invoice.lines if ln.id == line_id), None)
    if line is None:
        raise HTTPException(status_code=404, detail="invoice line not found")

    try:
        result = confirm_line(
            db,
            invoice,
            line,
            disposition=payload.disposition,
            company_id=_tid(request),
            actor_id=_actor(user),
            job_id=payload.job_id,
            inventory_item_id=payload.inventory_item_id,
            skip_reason=payload.skip_reason,
            update_catalog_cost=payload.update_catalog_cost,
        )
    except ConfirmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    maybe_mark_reviewed(db, invoice, _actor(user))
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=_tid(request),
        user_id=_actor(user),
        action="vendor_invoice_line_confirmed",
        entity_type="vendor_invoice_line",
        entity_id=str(line_id),
        details={"invoice_id": str(invoice_id), **{k: str(v) for k, v in result.items()}},
    )
    db.commit()
    return _detail(db, invoice)


def _load(db: Session, invoice_id: UUID) -> VendorInvoice:
    invoice = db.execute(
        select(VendorInvoice)
        .where(VendorInvoice.id == invoice_id)
        .where(VendorInvoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="vendor invoice not found")
    return invoice
