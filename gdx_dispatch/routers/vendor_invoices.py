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
    PAY_SOURCE_MANUAL,
    STATUS_PAID,
    STATUS_OPEN,
    VALID_STATUSES,
    VendorBillPayment,
    VendorInvoice,
)
from gdx_dispatch.modules.vendor_invoices.payments import (
    PaymentError,
    payment_summary,
    record_payment,
    void_payment,
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


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    amount: Decimal
    paid_date: date | None
    source: str
    reference: str | None
    match_id: UUID | None
    created_by: str | None
    created_at: datetime
    voided_at: datetime | None


class PaymentCreateIn(BaseModel):
    amount: Decimal
    paid_date: date | None = None
    reference: str | None = None


class InvoiceDetailOut(InvoiceSummaryOut):
    lines: list[LineOut] = []
    suggestions: list[JobSuggestionOut] = []
    invariant_ok: bool = True
    payments: list[PaymentOut] = []
    paid_total: float = 0.0
    open_balance: float = 0.0
    is_partial: bool = False


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
    all_payments = db.scalars(
        select(VendorBillPayment)
        .where(VendorBillPayment.vendor_invoice_id == invoice.id)
        .order_by(VendorBillPayment.created_at)
    ).all()
    return InvoiceDetailOut(
        **summary.model_dump(),
        lines=[LineOut.model_validate(ln) for ln in lines],
        suggestions=suggestions,
        payments=[PaymentOut.model_validate(p) for p in all_payments],
        **payment_summary(db, invoice),
        # M26 (money audit 2026-08-04): was `.startswith(...)`, but the service
        # joins note parts with "; " and puts the LLM marker FIRST — so a
        # rung-2 bill whose header arithmetic failed read
        # "LLM_EXTRACTED (...); INVARIANT_MISMATCH: ..." and startswith
        # returned False. The guard on untrusted LLM money reported PASS for
        # exactly the extraction path it exists to protect.
        invariant_ok="INVARIANT_MISMATCH" not in (invoice.notes or ""),
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


class PayableOut(InvoiceSummaryOut):
    paid_total: float = 0.0
    open_balance: float = 0.0
    is_partial: bool = False


@router.get(
    "/payables",
    response_model=list[PayableOut],
    dependencies=[Depends(require_permission("vendor_invoices.read"))],
)
async def list_payables(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PayableOut]:
    """Open (unpaid) bills, soonest due first — the cash-out picture.
    Partial payments exist now, so each row carries its true remaining
    ``open_balance``; consumers summing exposure must sum THAT, not
    ``total`` (a partially-paid bill stays 'open')."""
    stmt = (
        select(VendorInvoice)
        .where(VendorInvoice.deleted_at.is_(None))
        .where(VendorInvoice.status == STATUS_OPEN)
        .order_by(VendorInvoice.due_date.is_(None), VendorInvoice.due_date.asc())
    )
    rows = db.execute(stmt).scalars().all()
    paid_by_bill: dict = {}
    if rows:
        from sqlalchemy import func as _func

        for bill_id, paid in db.execute(
            select(VendorBillPayment.vendor_invoice_id, _func.sum(VendorBillPayment.amount))
            .where(
                VendorBillPayment.vendor_invoice_id.in_([r.id for r in rows]),
                VendorBillPayment.voided_at.is_(None),
            )
            .group_by(VendorBillPayment.vendor_invoice_id)
        ).all():
            paid_by_bill[bill_id] = paid or Decimal("0.00")
    out = []
    for r in rows:
        paid = paid_by_bill.get(r.id, Decimal("0.00"))
        total = r.total or Decimal("0.00")
        out.append(PayableOut(
            **InvoiceSummaryOut.model_validate(r).model_dump(),
            paid_total=float(paid),
            open_balance=float(total - paid),
            is_partial=bool(paid > 0 and paid < total),
        ))
    return out


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
        if payload.status == STATUS_PAID:
            # Single-writer rule (books-convergence Track 1): 'paid' is
            # DERIVED from payment records — record a payment instead. The
            # migration backfilled every historically Mark-paid bill, so no
            # legitimate caller needs this transition anymore.
            raise HTTPException(
                status_code=409,
                detail="status 'paid' is derived from payment records — "
                "record a payment via POST /{id}/payments instead",
            )
        if payload.status == "void":
            # Money against a void bill is a contradiction (diff-audit
            # SHOULD-FIX 5): the live payments would keep counting toward a
            # bill the office just declared never-owed. Void them first.
            from gdx_dispatch.modules.vendor_invoices.payments import live_payments

            if live_payments(db, invoice):
                raise HTTPException(
                    status_code=409,
                    detail="bill has recorded payments — void the payments "
                    "before voiding the bill",
                )
        invoice.status = payload.status
        if payload.status == STATUS_OPEN:
            # Reopening runs the derivation — a bill whose live payments
            # already cover the total lands back on 'paid', not a lie.
            from gdx_dispatch.modules.vendor_invoices.payments import recompute_status

            recompute_status(db, invoice)
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
    "/{invoice_id}/payments",
    response_model=InvoiceDetailOut,
    dependencies=[Depends(require_permission("vendor_invoices.write"))],
)
async def record_bill_payment(
    invoice_id: UUID,
    payload: PaymentCreateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvoiceDetailOut:
    """Record a manual payment against a bill — the replacement for the
    retired Mark-paid status PATCH. Capped at the open balance; the status
    derives from the records."""
    from gdx_dispatch.modules.ledger.engine import PeriodLockedError
    from gdx_dispatch.modules.vendor_invoices.payments import payment_refusal_message

    invoice = _load(db, invoice_id)
    try:
        payment = record_payment(
            db,
            invoice,
            amount=payload.amount,
            paid_date=payload.paid_date,
            source=PAY_SOURCE_MANUAL,
            reference=payload.reference,
            created_by=_actor(user),
        )
    except PaymentError as exc:
        raise HTTPException(status_code=409, detail=payment_refusal_message(exc)) from exc
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail="the payment date falls in a locked accounting period",
        ) from exc
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=_tid(request),
        user_id=_actor(user),
        action="vendor_bill_payment_recorded",
        entity_type="vendor_invoice",
        entity_id=str(invoice.id),
        details={
            "payment_id": str(payment.id),
            "amount": str(payment.amount),
            "paid_date": payment.paid_date.isoformat() if payment.paid_date else None,
            "status_after": invoice.status,
        },
    )
    db.commit()
    return _detail(db, invoice)


@router.post(
    "/{invoice_id}/payments/{payment_id}/void",
    response_model=InvoiceDetailOut,
    dependencies=[Depends(require_permission("vendor_invoices.write"))],
)
async def void_bill_payment(
    invoice_id: UUID,
    payment_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvoiceDetailOut:
    """Void one payment record (payments are never deleted or edited).
    Match-created payments refuse this while their bank match stays
    confirmed — unconfirm the match instead."""
    from gdx_dispatch.modules.ledger.engine import PeriodLockedError
    from gdx_dispatch.modules.vendor_invoices.payments import payment_refusal_message

    invoice = _load(db, invoice_id)
    payment = db.get(VendorBillPayment, payment_id)
    if payment is None or payment.vendor_invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="payment not found on this bill")
    try:
        void_payment(db, payment, voided_by=_actor(user))
    except PaymentError as exc:
        raise HTTPException(status_code=409, detail=payment_refusal_message(exc)) from exc
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail="unwinding this payment would post into a locked accounting period",
        ) from exc
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=_tid(request),
        user_id=_actor(user),
        action="vendor_bill_payment_voided",
        entity_type="vendor_invoice",
        entity_id=str(invoice.id),
        details={"payment_id": str(payment.id), "status_after": invoice.status},
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
    except Exception as exc:
        # GL symmetry seam (plan-audit MUST-FIX 9a): with posting enabled,
        # confirm now posts P5 — ledger refusals must surface as 409s with
        # the reason (the expenses router's _post_or_409 contract), never
        # turn bill confirms into bare 500s.
        from gdx_dispatch.modules.ledger.engine import PeriodLockedError
        from gdx_dispatch.modules.ledger.rules import ExpenseCompositionError

        if isinstance(exc, ExpenseCompositionError):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, PeriodLockedError):
            raise HTTPException(
                status_code=409,
                detail=f"expense date falls in a locked accounting period — {exc}",
            ) from exc
        raise

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
