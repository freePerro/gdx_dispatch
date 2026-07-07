"""Sprint tech_mobile Phase 2.2 — On-Site Invoicing.

Mobile-scoped wrappers around invoices so techs can generate and email
an invoice from the truck immediately after job completion. No payment
capture (deferred to a future sprint per the sprint plan); office still
reconciles the money side.

Endpoints (all under /api/mobile, gated on the "mobile" module):

    POST /api/mobile/jobs/{job_id}/invoice         — create invoice from
                                                      completed job + accepted quote
    GET  /api/mobile/jobs/{job_id}/financial       — financial summary at close-out
    POST /api/mobile/invoices/{invoice_id}/send    — email invoice to customer
    POST /api/mobile/invoices/{invoice_id}/send-receipt — send receipt for an
                                                          office-recorded payment
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID as _UUID
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as _text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except Exception:
    log.exception("mobile_invoicing_auth_import_failed_using_fallback")

    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile-invoicing"],
    dependencies=[Depends(require_module("mobile"))],
)


def _jr(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


def _money(v: Decimal | float | str) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _tenant_id(request: Request) -> str:
    state = getattr(request, "state", None)
    tenant = getattr(state, "tenant", None) or {}
    return str(tenant.get("id") or getattr(state, "tenant_id", "") or "")


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "")


def _job_belongs_to_tech(db: Session, job_id: str, user_id: str) -> bool:
    if not job_id or not user_id:
        return False
    row = db.execute(
        _text(
            """
            SELECT 1 FROM jobs
            WHERE id = :jid AND deleted_at IS NULL AND assigned_to = :uid
            LIMIT 1
            """
        ),
        {"jid": job_id, "uid": user_id},
    ).scalar()
    if row:
        return True
    row = db.execute(
        _text(
            """
            SELECT 1 FROM job_assignments ja
            JOIN technicians t ON t.id = ja.tech_id
            WHERE ja.job_id = :jid AND CAST(t.user_id AS TEXT) = :uid AND t.active IS NOT FALSE
            LIMIT 1
            """
        ),
        {"jid": job_id, "uid": user_id},
    ).scalar()
    return bool(row)


def _next_invoice_number(db: Session) -> str:
    row = db.execute(
        _text("SELECT invoice_number FROM invoices ORDER BY created_at DESC LIMIT 1")
    ).first()
    if row and row[0] and row[0].startswith("INV-"):
        try:
            n = int(row[0].split("-", 1)[1]) + 1
            return f"INV-{n:06d}"
        except (ValueError, AttributeError):
            pass
    return f"INV-{datetime.now(UTC):%y%m}{secrets.token_hex(2).upper()}"


def _serialize_invoice(inv: Invoice, *, include_lines: bool = False, db: Session | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(inv.id),
        "invoice_number": inv.invoice_number,
        "job_id": str(inv.job_id) if inv.job_id else None,
        "customer_id": str(inv.customer_id) if inv.customer_id else None,
        "status": inv.status,
        "subtotal": float(inv.subtotal or 0),
        "tax_amount": float(inv.tax_amount or 0),
        "total": float(inv.total or 0),
        "balance_due": float(inv.balance_due or 0),
        "amount_paid": float(inv.amount_paid or 0) if inv.amount_paid is not None else 0.0,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "sent_at": inv.sent_at.isoformat() if inv.sent_at else None,
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
        "notes": inv.notes,
    }
    if include_lines and db is not None:
        rows = db.execute(
            _text(
                """
                SELECT id, description, quantity, unit_price, line_total, sort_order
                FROM invoice_lines
                WHERE invoice_id = :iid OR invoice_id = :iid_dashed
                ORDER BY sort_order ASC
                """
            ),
            {"iid": inv.id.hex if hasattr(inv.id, "hex") else str(inv.id).replace("-", ""),
             "iid_dashed": str(inv.id)},
        ).all()
        out["lines"] = [
            {
                "id": str(r[0]),
                "description": r[1],
                "quantity": int(r[2] or 0),
                "unit_price": float(r[3] or 0),
                "line_total": float(r[4] or 0),
                "sort_order": int(r[5] or 0),
            }
            for r in rows
        ]
    return out


# ---------------------------------------------------------------------------
# Pydantic input shapes
# ---------------------------------------------------------------------------


class CreateInvoiceIn(BaseModel):
    """Body for POST /api/mobile/jobs/{job_id}/invoice.

    estimate_id optional — when provided, the invoice is built from the
    accepted quote's lines. If absent, the invoice is created with no
    lines (office can add lines later via the existing /api/invoices
    routes).
    """

    estimate_id: str | None = Field(default=None)
    notes: str | None = Field(default=None, max_length=5000)
    send_email: bool = Field(default=True)


class SendReceiptIn(BaseModel):
    """Body for POST /api/mobile/invoices/{invoice_id}/send-receipt.

    Optional payment_id when receipting a specific payment; when absent,
    the most recent payment on the invoice is used.
    """

    payment_id: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# GET /api/mobile/jobs/{job_id}/financial — close-out summary
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/financial", response_model=None)
def job_financial_summary(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Per-job financial summary at close-out.

    Returns:
        - parts_cost: sum of estimated cost on parts_needed rows for this job
        - labor_hours: total labor hours from time entries
        - accepted_quote: { id, total } if any accepted quote on this job
        - invoices: [{id, invoice_number, total, status, balance_due}]
        - payment_status: 'pending' (mobile-side default) | 'paid' (any
          invoice paid_at set)
    """
    user = current_user or {}
    user_id = _user_id(user)
    if not _job_belongs_to_tech(db, job_id, user_id):
        return _jr({"detail": "job not found or not assigned to you"}, 404)

    # parts_needed is in inventory module, optional in some tenant DBs.
    try:
        parts_cost = float(db.execute(
            _text(
                """
                SELECT COALESCE(SUM(COALESCE(estimated_cost, 0)), 0)
                FROM parts_needed
                WHERE job_id = :jid AND deleted_at IS NULL
                """
            ),
            {"jid": job_id},
        ).scalar() or 0)
    except Exception:
        log.exception("financial_summary_parts_cost_unavailable job=%s", job_id)
        parts_cost = 0.0

    # time_entries / timeclock_entries: portable across SQLite + PG.
    try:
        labor_hours = float(db.execute(
            _text(
                """
                SELECT COALESCE(SUM(
                    (julianday(clock_out) - julianday(clock_in)) * 24.0
                ), 0)
                FROM timeclock_entries
                WHERE job_id = :jid AND clock_out IS NOT NULL
                """
            ),
            {"jid": job_id},
        ).scalar() or 0)
    except Exception:
        log.exception("financial_summary_labor_hours_unavailable job=%s", job_id)
        labor_hours = 0.0

    accepted = db.execute(
        select(Estimate)
        .where(
            Estimate.job_id == _UUID(job_id),
            Estimate.status == "accepted",
            Estimate.deleted_at.is_(None),
        )
        .order_by(Estimate.accepted_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    invoices = db.execute(
        select(Invoice)
        .where(Invoice.job_id == _UUID(job_id), Invoice.deleted_at.is_(None))
        .order_by(Invoice.created_at.desc())
    ).scalars().all()

    any_paid = any(inv.paid_at is not None for inv in invoices)
    payment_status = "paid" if any_paid else ("pending" if invoices else "no_invoice")

    return _jr({
        "job_id": job_id,
        "parts_cost": parts_cost,
        "labor_hours": round(labor_hours, 2),
        "accepted_quote": (
            {"id": str(accepted.id), "total": float(accepted.total or 0), "estimate_number": accepted.estimate_number}
            if accepted else None
        ),
        "invoices": [
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "total": float(inv.total or 0),
                "balance_due": float(inv.balance_due or 0),
                "status": inv.status,
                "sent_at": inv.sent_at.isoformat() if inv.sent_at else None,
            }
            for inv in invoices
        ],
        "payment_status": payment_status,
    })


# ---------------------------------------------------------------------------
# POST /api/mobile/jobs/{job_id}/invoice
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/invoice", response_model=None, status_code=201)
def mobile_create_invoice(
    job_id: str,
    payload: CreateInvoiceIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create + (optionally) email an invoice from the truck.

    Mirrors /api/invoices POST but tech-scoped: the calling user must
    own the job. When ``estimate_id`` is supplied (must be ``accepted``
    and on the same job), lines are copied from the estimate.
    """
    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, job_id, user_id):
        return _jr({"detail": "job not found or not assigned to you"}, 404)

    # Raw SQL — avoids SQLAlchemy Uuid type quirks across SQLite/PG when
    # the seed used a hyphenated UUID string vs. a 32-char hex.
    job_row = db.execute(
        _text(
            "SELECT customer_id FROM jobs WHERE id = :jid AND deleted_at IS NULL"
        ),
        {"jid": job_id},
    ).first()
    if job_row is None:
        return _jr({"detail": "job not found"}, 404)
    job_customer_id = job_row[0]

    # Estimate (optional — if present must be accepted + on this job).
    estimate: Estimate | None = None
    if payload.estimate_id:
        estimate = db.execute(
            select(Estimate).where(
                Estimate.id == _UUID(payload.estimate_id),
                Estimate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if estimate is None or estimate.job_id != _UUID(job_id):
            return _jr({"detail": "estimate not found for this job"}, 404)
        if estimate.status != "accepted":
            return _jr(
                {"detail": f"estimate must be 'accepted' to invoice; current: {estimate.status}"},
                409,
            )

    customer_id = None
    if job_customer_id is not None:
        if isinstance(job_customer_id, _UUID):
            customer_id = job_customer_id
        else:
            try:
                customer_id = _UUID(str(job_customer_id))
            except (ValueError, AttributeError):
                customer_id = None
    if customer_id is None:
        # 2026-05-11 — invoices.customer_id is NOT NULL. Refuse with a 400
        # so the tech app surfaces a clear message rather than a 500 from
        # the NOT NULL violation at db.flush().
        return _jr(
            {"detail": "Job has no customer assigned — set a customer before invoicing"},
            400,
        )
    invoice_date_value = date.today()
    due_date_value = invoice_date_value + timedelta(days=30)

    if estimate is not None:
        from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
        _t = compute_estimate_totals(estimate, db)
        subtotal_value = _t["subtotal"]
        tax_amount_value = _t["tax"]
        total_value = _t["total"]
    else:
        subtotal_value = 0.0
        tax_amount_value = 0.0
        total_value = 0.0

    invoice = Invoice(
        id=uuid4(),
        job_id=_UUID(job_id),
        invoice_number=_next_invoice_number(db),
        billing_type="standard",
        sequence_number=1,
        subtotal=_money(subtotal_value),
        tax_amount=_money(tax_amount_value),
        total=_money(total_value),
        balance_due=_money(total_value),
        status="draft",
        invoice_date=invoice_date_value,
        due_date=due_date_value,
        notes=(payload.notes.strip() if payload.notes else None),
        public_token=secrets.token_urlsafe(48)[:64],
        locked=False,
        customer_id=customer_id,
        company_id=str(tenant_id),
    )
    db.add(invoice)
    db.flush()

    # Copy estimate → invoice lines via raw SQL (portable across SQLite/PG).
    # Two paths:
    #   - Proposal mode (accepted_tier_id set): single summary line per the
    #     chosen tier — the customer signed against ONE tier total, so the
    #     invoice mirrors that without ambiguity. Per-tier sub-lines stay
    #     on the Estimate for audit / commission analytics.
    #   - Plain mode: copy every estimate line verbatim (legacy behaviour).
    if estimate:
        if estimate.accepted_tier_id is not None:
            tid_val = estimate.accepted_tier_id
            tid_str = tid_val.hex if hasattr(tid_val, "hex") else str(tid_val).replace("-", "")
            tier_row = db.execute(
                _text(
                    """
                    SELECT tier_name, description, total_price
                    FROM proposal_tiers
                    WHERE id = :tid OR id = :tid_dashed
                    """
                ),
                {"tid": tid_str, "tid_dashed": str(tid_val)},
            ).first()
            if tier_row is not None:
                tier_name, tier_desc, tier_total = tier_row
                price = _money(tier_total or 0)
                # PR1-billing-capture: F-75 zero-price policy applies from
                # the truck too (this path bypassed the desktop guard).
                # Block-only — the tech app has no warning-banner surface,
                # so warn-mode is intentionally desktop-only.
                if float(price) <= 0:
                    from gdx_dispatch.modules.catalog_policy import get_policy
                    if get_policy(str(tenant_id)).block_zero_price_on_invoice:
                        db.rollback()
                        return _jr(
                            {"detail": "accepted tier has no price — price it before invoicing (tenant policy blocks zero-price invoice lines)"},
                            422,
                        )
                desc = (
                    f"{(estimate.label or 'Service').strip()} — "
                    f"{tier_name.title()} Tier"
                    + (f" ({tier_desc})" if tier_desc else "")
                )
                db.add(
                    InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description=desc[:500],
                        quantity=1,
                        unit_price=price,
                        line_total=price,
                        sort_order=1,
                        company_id=str(tenant_id),
                    )
                )
                invoice.subtotal = price
                invoice.total = price
                invoice.balance_due = price
        else:
            # Plain estimate — copy all lines.
            line_rows = db.execute(
                _text(
                    """
                    SELECT description, quantity, unit_price, line_total, sort_order
                    FROM estimate_lines
                    WHERE estimate_id = :eid
                    ORDER BY sort_order ASC
                    """
                ),
                {"eid": str(estimate.id)},
            ).all()
            # PR1-billing-capture: same block-only F-75 guard as the tier
            # path — a $0 estimate line must not slip onto an invoice from
            # the truck when the tenant blocks zero-price invoicing.
            if any(float(ln[2] or 0) <= 0 for ln in line_rows):
                from gdx_dispatch.modules.catalog_policy import get_policy
                if get_policy(str(tenant_id)).block_zero_price_on_invoice:
                    db.rollback()
                    return _jr(
                        {"detail": "estimate contains a zero-price line — price it before invoicing (tenant policy blocks zero-price invoice lines)"},
                        422,
                    )
            for ln in line_rows:
                db.add(
                    InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        description=ln[0],
                        quantity=int(ln[1]),
                        unit_price=_money(ln[2]),
                        line_total=_money(ln[3]),
                        sort_order=int(ln[4]),
                        company_id=str(tenant_id),
                    )
                )
            db.flush()
            new_subtotal = float(db.execute(
                _text("SELECT COALESCE(SUM(line_total), 0) FROM invoice_lines WHERE invoice_id = :iid"),
                {"iid": str(invoice.id)},
            ).scalar() or 0)
            invoice.subtotal = _money(new_subtotal)
            invoice.total = _money(new_subtotal)
            invoice.balance_due = _money(new_subtotal)

    db.commit()
    db.refresh(invoice)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_invoice_created",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={
            "job_id": job_id,
            "estimate_id": str(estimate.id) if estimate else None,
            "subtotal": float(invoice.subtotal or 0),
        },
        request=request,
    )
    db.commit()

    # Optionally send the invoice email immediately (S2-B2).
    if payload.send_email:
        _send_invoice_email(db, invoice, tenant_id=tenant_id)
        invoice.status = "sent"
        invoice.sent_at = datetime.now(UTC)
        db.commit()
        db.refresh(invoice)

    return _jr(_serialize_invoice(invoice, include_lines=True, db=db), 201)


def _send_invoice_email(
    db: Session,
    invoice: Invoice,
    *,
    tenant_id: str,
    user_id: str | None = None,
) -> None:
    """Send the invoice email to the customer, if email available.

    Mirrors the path in invoices.send_estimate (lines 743–795) but for
    invoices. Failures are logged not raised — invoice creation already
    succeeded; "email failed" must not undo the financial record.
    """
    try:
        cust = None
        if invoice.customer_id is not None:
            cust = db.execute(
                select(Customer).where(
                    Customer.id == invoice.customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        if cust is None or not cust.email:
            log.info("mobile_invoice_email_skipped no_customer_email invoice=%s", invoice.id)
            return

        from gdx_dispatch.core.transactional_email import send_transactional_email
        try:
            from gdx_dispatch.core.email_sender import build_invoice_email_html  # type: ignore[attr-defined]
        except Exception:
            build_invoice_email_html = None  # type: ignore[assignment]

        # Get company name
        company_name = "Your Service Company"
        try:
            from gdx_dispatch.models.tenant_models import AppSettings
            settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
            if settings_obj and getattr(settings_obj, "company_name", None):
                company_name = settings_obj.company_name
        except Exception:
            log.exception("mobile_invoice_email_company_name_lookup_failed")

        # Build a simple email body if no specialised builder exists.
        if build_invoice_email_html is not None:
            lines = db.execute(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == invoice.id)
                .order_by(InvoiceLine.sort_order.asc())
            ).scalars().all()
            html = build_invoice_email_html(  # type: ignore[misc]
                company_name=company_name,
                invoice_number=invoice.invoice_number,
                customer_name=cust.name or "Valued Customer",
                line_items=[
                    {
                        "description": ln.description,
                        "quantity": ln.quantity,
                        "unit_price": float(ln.unit_price or 0),
                        "line_total": float(ln.line_total or 0),
                    }
                    for ln in lines
                ],
                total=float(invoice.total or 0),
                notes=invoice.notes or "",
            )
        else:
            html = (
                f"<p>Hi {cust.name or 'there'},</p>"
                f"<p>Please find your invoice <strong>#{invoice.invoice_number}</strong> "
                f"from {company_name} attached.</p>"
                f"<p><strong>Total: ${float(invoice.total or 0):,.2f}</strong></p>"
                f"<p>Due {invoice.due_date.isoformat() if invoice.due_date else 'on receipt'}.</p>"
            )
        send_transactional_email(
            tenant_db=db,
            tenant_id=str(tenant_id),
            user_id=str(user_id) if user_id else None,
            to_email=cust.email,
            to_name=cust.name or "",
            subject=f"Invoice #{invoice.invoice_number} from {company_name}",
            html_body=html,
        )
    except Exception:
        log.exception("mobile_invoice_email_failed invoice=%s", invoice.id)


# ---------------------------------------------------------------------------
# POST /api/mobile/invoices/{invoice_id}/send
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/send", response_model=None)
def mobile_send_invoice(
    invoice_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Re-send the invoice email (idempotent — updates sent_at)."""
    invoice = db.execute(
        select(Invoice).where(Invoice.id == _UUID(invoice_id), Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if invoice is None:
        return _jr({"detail": "invoice not found"}, 404)

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, str(invoice.job_id), user_id):
        return _jr({"detail": "invoice not on a job assigned to you"}, 403)

    # PR1-billing-capture (audit catch): the desktop /send now 409s on void,
    # but this path still EMAILED voided invoices to customers. Same guard.
    if invoice.status == "void":
        return _jr({"detail": "invoice is void — it cannot be re-sent"}, 409)

    _send_invoice_email(db, invoice, tenant_id=tenant_id)
    invoice.status = "sent" if invoice.status == "draft" else invoice.status
    invoice.sent_at = datetime.now(UTC)
    db.commit()
    db.refresh(invoice)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_invoice_sent",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"invoice_number": invoice.invoice_number},
        request=request,
    )
    db.commit()
    return _jr(_serialize_invoice(invoice))


# ---------------------------------------------------------------------------
# POST /api/mobile/invoices/{invoice_id}/send-receipt
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/send-receipt", response_model=None)
def mobile_send_receipt(
    invoice_id: str,
    payload: SendReceiptIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send a payment receipt for an office-recorded payment.

    Mobile cannot record payments (deferred — Stripe / cash capture is
    Sprint future). This is for the case where dispatch / office took
    a payment over the phone and the tech wants the customer to get a
    receipt in their inbox before leaving.
    """
    invoice = db.execute(
        select(Invoice).where(Invoice.id == _UUID(invoice_id), Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if invoice is None:
        return _jr({"detail": "invoice not found"}, 404)

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, str(invoice.job_id), user_id):
        return _jr({"detail": "invoice not on a job assigned to you"}, 403)

    # Find the payment to receipt — explicit payment_id wins, else most recent.
    payment_row = None
    if payload.payment_id:
        payment_row = db.execute(
            _text(
                """
                SELECT id, amount, method, payment_date, reference
                FROM payments
                WHERE id = :pid AND invoice_id = :iid
                """
            ),
            {"pid": payload.payment_id, "iid": invoice_id},
        ).first()
    else:
        payment_row = db.execute(
            _text(
                """
                SELECT id, amount, method, payment_date, reference
                FROM payments
                WHERE invoice_id = :iid
                ORDER BY payment_date DESC, created_at DESC
                LIMIT 1
                """
            ),
            {"iid": invoice_id},
        ).first()

    if payment_row is None:
        return _jr(
            {"detail": "no payment found to receipt; office must record the payment first"},
            404,
        )

    # Best-effort email — we re-use the invoice email path with a "receipt"
    # subject prefix. A dedicated receipt template can land later.
    try:
        cust = None
        if invoice.customer_id is not None:
            cust = db.execute(
                select(Customer).where(
                    Customer.id == invoice.customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        if cust and cust.email:
            from gdx_dispatch.core.email_sender import send_email
            html = (
                f"<p>Hi {cust.name or 'there'},</p>"
                f"<p>Thank you for your payment of "
                f"<strong>${float(payment_row[1] or 0):,.2f}</strong> "
                f"on invoice <strong>#{invoice.invoice_number}</strong>.</p>"
                f"<p>Method: {payment_row[2]}<br>"
                f"Date: {payment_row[3].isoformat() if payment_row[3] else 'today'}</p>"
            )
            send_email(
                db,
                str(tenant_id),
                cust.email,
                f"Receipt for invoice #{invoice.invoice_number}",
                html,
                cust.name,
            )
    except Exception:
        log.exception("mobile_send_receipt_failed invoice=%s", invoice.id)
        return _jr({"detail": "receipt email failed"}, 500)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_invoice_receipt_sent",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"payment_id": str(payment_row[0])},
        request=request,
    )
    db.commit()
    return _jr({"sent": True, "invoice_id": str(invoice.id), "payment_id": str(payment_row[0])})
