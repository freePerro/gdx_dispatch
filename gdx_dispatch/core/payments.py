"""Stripe Elements embedded payment collection + ACH bank transfer support.

Endpoints
---------
POST /api/payments/create-intent   — create PaymentIntent for an invoice
POST /api/payments/confirm         — confirm payment after Stripe.js completes
POST /api/payments/ach/setup       — create SetupIntent for ACH bank account
POST /api/payments/ach/charge      — charge a saved ACH payment method
GET  /api/payments/methods         — list saved payment methods for a customer
DELETE /api/payments/methods/{pm_id} — remove a saved payment method

Public (no auth):
GET  /pay/{invoice_token}          — serve the Stripe Elements payment form

Webhook helper (called from stripe_webhook router):
    handle_payment_webhook(event, db)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Invoice, Payment

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

router = APIRouter(prefix="/api/payments", tags=["payments"])
public_router = APIRouter(tags=["payments-public"])


@router.get("", operation_id="api_list_payments")
def list_payments(
    source: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
) -> dict:
    """Tenant-wide payment list. PaymentsView.vue calls this on mount.

    `source` filter:
        - "quickbooks" → only Payment.method == "quickbooks" (C6)
        - "manual"     → everything else
        - None         → all payments
    """
    from sqlalchemy import select as _select

    stmt = _select(Payment, Invoice).join(Invoice, Payment.invoice_id == Invoice.id)
    if source == "quickbooks":
        stmt = stmt.where(Payment.method == "quickbooks")
    elif source == "manual":
        stmt = stmt.where(Payment.method != "quickbooks")
    stmt = stmt.order_by(Payment.payment_date.desc(), Payment.created_at.desc()).limit(max(1, min(limit, 1000)))

    items = []
    for payment, invoice in db.execute(stmt).all():
        items.append({
            "id": str(payment.id),
            "invoice_id": str(payment.invoice_id),
            "invoice_number": invoice.invoice_number if invoice else None,
            "amount": float(payment.amount or 0),
            "method": payment.method,
            # Derived `source` so the UI can filter without re-encoding the
            # method-string convention. quickbooks => imported; else manual.
            "source": "quickbooks" if payment.method == "quickbooks" else "manual",
            "date": payment.payment_date.isoformat() if payment.payment_date else None,
            "created_at": payment.created_at.isoformat() if payment.created_at else None,
        })
    return {"items": items, "total": len(items)}

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class CreateIntentRequest(BaseModel):
    invoice_id: str
    amount: int  # cents
    currency: str = "usd"


class ConfirmPaymentRequest(BaseModel):
    payment_intent_id: str
    invoice_id: str


class ACHSetupRequest(BaseModel):
    customer_email: str


class ACHChargeRequest(BaseModel):
    payment_method_id: str
    invoice_id: str
    amount: int  # cents


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_stripe() -> None:
    """Set Stripe API key from environment."""
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")


def _stripe_extra(tenant: dict) -> dict[str, Any]:
    """Return Stripe Connect kwargs if tenant has a connected account."""
    acct = tenant.get("stripe_connect_account_id")
    return {"stripe_account": acct} if acct else {}


def _mark_invoice_paid(
    invoice: Invoice,
    db: Session,
    *,
    external_ref: str | None = None,
    method: str = "card",
    amount: float | None = None,
) -> None:
    """Record processor money as a REAL Payment row, recalc, post P3.

    GL S6 rewrite (bug #1, GL audit §12): the old version flipped the status
    straight to paid with NO Payment row and a mid-flow commit — money moved
    at the processor with nothing recorded locally. Now:

    - idempotent on ``external_ref`` (the PaymentIntent id): confirm +
      webhook both firing records exactly one payment;
    - the status flip happens inside ``_recalculate_invoice`` via the
      chokepoint (auto-flip), so P1 posts before P3 when the ledger is on;
    - one commit at the end — the payment, the recalc, and the ledger entry
      land or roll back together.
    """
    from sqlalchemy import select as _select

    # Late imports: the single recalc/posting truths live one layer up/over;
    # importing at call time avoids a routers←core import at module load.
    from gdx_dispatch.modules.ledger.rules import post_payment_received
    from gdx_dispatch.routers.invoices import _recalculate_invoice

    if external_ref:
        existing = db.scalars(
            _select(Payment).where(
                Payment.invoice_id == invoice.id,
                Payment.reference == external_ref,
            )
        ).first()
        if existing is not None:
            return  # already recorded (idempotent across confirm + webhook)

    from sqlalchemy import func as _func

    already_paid = db.execute(
        _select(_func.sum(Payment.amount)).where(
            Payment.invoice_id == invoice.id, Payment.voided_at.is_(None)
        )
    ).scalar_one_or_none() or 0
    remaining = float(invoice.total or 0) - float(already_paid)
    # amount = what the processor says MOVED (PaymentIntent amount /
    # amount_received, audit round 2: recording "remaining" instead of the
    # actual charge misstated cash on partial intents). Zero/None → fall
    # back to remaining (legacy events without an amount).
    pay_amount = amount if amount else max(remaining, 0)
    if pay_amount <= 0:
        _recalculate_invoice(invoice, db)  # nothing new to record; true-up status
        db.commit()
        return

    payment = Payment(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        amount=pay_amount,
        method=method,
        payment_date=datetime.now(timezone.utc).date(),
        reference=external_ref,
    )
    db.add(payment)
    db.flush()
    _recalculate_invoice(invoice, db)
    post_payment_received(db, payment, invoice)
    db.commit()


# ---------------------------------------------------------------------------
# POST /api/payments/create-intent
# ---------------------------------------------------------------------------

@router.post("/create-intent")
def create_intent(
    body: CreateIntentRequest,
    request: Request,
    db: Session = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> dict:
    """Create a Stripe PaymentIntent for an invoice.

    Idempotency key ``gdx-pi-{invoice_id}`` prevents duplicate charges if the
    client retries the same request.
    """
    _init_stripe()
    try:
        invoice_uuid = UUID(body.invoice_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid invoice_id format") from None

    invoice = db.get(Invoice, invoice_uuid)
    if not invoice or invoice.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        pi = stripe.PaymentIntent.create(
            amount=body.amount,
            currency=body.currency,
            metadata={
                "invoice_id": body.invoice_id,
                "tenant_id": str(tenant.get("id", "")),
            },
            idempotency_key=f"gdx-pi-{body.invoice_id}",
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe create_intent error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {"client_secret": pi.client_secret, "payment_intent_id": pi.id}


# ---------------------------------------------------------------------------
# POST /api/payments/confirm
# ---------------------------------------------------------------------------

@router.post("/confirm")
def confirm_payment(
    body: ConfirmPaymentRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> dict:
    """Confirm payment after Stripe.js reports success.

    Retrieves the PaymentIntent from Stripe and, if its status is
    ``succeeded``, marks the local invoice as paid.
    """
    _init_stripe()
    try:
        pi = stripe.PaymentIntent.retrieve(body.payment_intent_id)
    except stripe.StripeError as exc:
        logger.error("Stripe retrieve error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    if pi.status == "succeeded":
        try:
            invoice_uuid = UUID(body.invoice_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="Invalid invoice_id format") from None

        invoice = db.get(Invoice, invoice_uuid)
        if invoice and invoice.deleted_at is None:
            _mark_invoice_paid(invoice, db, external_ref=pi.id, method="card", amount=(pi.amount or 0) / 100.0)

    return {"status": pi.status, "invoice_id": body.invoice_id}


# ---------------------------------------------------------------------------
# POST /api/payments/ach/setup
# ---------------------------------------------------------------------------

@router.post("/ach/setup")
def ach_setup(
    body: ACHSetupRequest,
    request: Request,
    token: str | None = Depends(oauth2_scheme),
) -> dict:
    """Create a SetupIntent so Stripe.js can collect ACH bank account details.

    Returns a ``client_secret`` that the frontend passes to
    ``stripe.collectBankAccountForSetup()``.
    """
    _init_stripe()
    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        si = stripe.SetupIntent.create(
            payment_method_types=["us_bank_account"],
            metadata={"email": body.customer_email, "tenant_id": str(tenant.get("id", ""))},
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe ach_setup error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {"client_secret": si.client_secret}


# ---------------------------------------------------------------------------
# POST /api/payments/ach/charge
# ---------------------------------------------------------------------------

@router.post("/ach/charge")
def ach_charge(
    body: ACHChargeRequest,
    request: Request,
    db: Session = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> dict:
    """Charge a saved ACH bank account payment method.

    Creates a PaymentIntent with ``confirm=True`` so the charge is initiated
    immediately. ACH payments are typically pending for 1-2 business days.
    """
    _init_stripe()
    try:
        invoice_uuid = UUID(body.invoice_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid invoice_id format") from None

    invoice = db.get(Invoice, invoice_uuid)
    if not invoice or invoice.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        pi = stripe.PaymentIntent.create(
            amount=body.amount,
            currency="usd",
            payment_method=body.payment_method_id,
            payment_method_types=["us_bank_account"],
            confirm=True,
            metadata={
                "invoice_id": body.invoice_id,
                "tenant_id": str(tenant.get("id", "")),
            },
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe ach_charge error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    # ACH payments may be processing (not yet succeeded); mark partial if needed
    if pi.status == "succeeded":
        _mark_invoice_paid(invoice, db, external_ref=pi.id, method="ach", amount=(pi.amount or 0) / 100.0)

    return {"status": pi.status, "payment_intent_id": pi.id}


# ---------------------------------------------------------------------------
# GET /api/payments/methods
# ---------------------------------------------------------------------------

@router.get("/methods", operation_id="api_list_payment_methods")
def list_methods(
    customer_id: str,
    request: Request,
    token: str | None = Depends(oauth2_scheme),
) -> dict:
    """List all saved payment methods (card + ACH) for a Stripe customer."""
    _init_stripe()
    tenant: dict = getattr(request.state, "tenant", {}) or {}
    extra = _stripe_extra(tenant)

    methods: list[dict] = []
    for pm_type in ("card", "us_bank_account"):
        try:
            page = stripe.PaymentMethod.list(customer=customer_id, type=pm_type, **extra)
            methods.extend(page.data)
        except stripe.StripeError as exc:
            logger.warning("Stripe list_methods (%s) error: %s", pm_type, exc)

    return {
        "methods": [
            {
                "id": pm.id,
                "type": pm.type,
                "card": pm.card.to_dict() if pm.type == "card" and pm.card else None,
                "us_bank_account": (
                    pm.us_bank_account.to_dict()
                    if pm.type == "us_bank_account" and pm.us_bank_account
                    else None
                ),
                "created": pm.created,
            }
            for pm in methods
        ]
    }


# ---------------------------------------------------------------------------
# DELETE /api/payments/methods/{pm_id}
# ---------------------------------------------------------------------------

@router.delete("/methods/{pm_id}", operation_id="api_delete_payment_method")
def delete_method(
    pm_id: str,
    token: str | None = Depends(oauth2_scheme),
) -> dict:
    """Detach (remove) a saved payment method from the Stripe customer."""
    _init_stripe()
    try:
        stripe.PaymentMethod.detach(pm_id)
    except stripe.StripeError as exc:
        logger.error("Stripe detach error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {"status": "detached", "pm_id": pm_id}


# ---------------------------------------------------------------------------
# GET /pay/{invoice_token}  — public, no auth
# ---------------------------------------------------------------------------

@public_router.get("/pay/{invoice_token}", response_class=HTMLResponse)
def pay_invoice(
    invoice_token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Serve the Stripe Elements payment form for a public invoice link.

    The invoice is looked up by its ``public_token`` (a unique random string
    sent to customers in payment-request emails). No authentication is
    required — the token itself acts as the secret.
    """
    invoice = (
        db.query(Invoice)
        .filter(Invoice.public_token == invoice_token, Invoice.deleted_at.is_(None))
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found or expired")

    return templates.TemplateResponse(
        "payment_form.html",
        {
            "request": request,
            "invoice": invoice,
            "stripe_publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
        },
    )


# ---------------------------------------------------------------------------
# Webhook helper (called from gdx_dispatch/routers/stripe_webhook.py)
# ---------------------------------------------------------------------------

def handle_payment_webhook(event: dict, db: Session) -> dict:
    """Process Stripe payment events from the webhook router.

    This function is called by the existing stripe_webhook router after
    deduplication. It handles per-invoice payment lifecycle events.

    Returns a dict with ``{"status": ...}`` for logging/response purposes.
    """
    event_type: str = event.get("type", "")
    data: dict = event.get("data", {}).get("object", {})

    if event_type == "payment_intent.succeeded":
        invoice_id: str = (data.get("metadata") or {}).get("invoice_id", "")
        if invoice_id:
            try:
                invoice = db.get(Invoice, UUID(invoice_id))
                if invoice and invoice.deleted_at is None and invoice.status != "paid":
                    _mark_invoice_paid(invoice, db, external_ref=data.get("id"), method="card", amount=(data.get("amount_received") or 0) / 100.0)
                    logger.info("Invoice %s marked paid via webhook", invoice_id)
                    # Receipt email placeholder — wire up notification service here
                    # send_receipt_email(invoice)
                    return {"status": "paid", "invoice_id": invoice_id}
            except Exception as exc:
                logger.error("handle_payment_webhook succeeded error: %s", exc)
                return {"status": "error", "detail": str(exc)}
        return {"status": "no_invoice_id"}

    if event_type == "payment_intent.payment_failed":
        invoice_id = (data.get("metadata") or {}).get("invoice_id", "")
        failure_msg = data.get("last_payment_error", {}).get("message", "unknown")
        logger.warning(
            "PaymentIntent failed for invoice %s: %s",
            invoice_id,
            failure_msg,
        )
        # Tenant notification placeholder — wire up notification service here
        # notify_tenant_payment_failed(invoice_id, failure_msg)
        return {"status": "failed", "invoice_id": invoice_id, "reason": failure_msg}

    return {"status": "ignored"}
