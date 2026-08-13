"""Stripe Elements embedded payment collection + ACH bank transfer support.

These endpoints back the anonymous ``/pay/{token}`` page, so they have no user
to authenticate. Authorization is structural instead — see the "Public-payment
authorization" section below: the caller proves which invoice they may touch by
presenting its token, and the amount, the intent↔invoice binding and the ACH
method↔invoice binding are all decided server-side.

Endpoints (all take ``invoice_token``)
--------------------------------------
POST /api/payments/create-intent   — create PaymentIntent for an invoice
POST /api/payments/confirm         — confirm payment after Stripe.js completes
POST /api/payments/ach/setup       — create SetupIntent for ACH bank account
POST /api/payments/ach/charge      — charge the bank account collected for it

Saved-payment-method management lives on the AUTHENTICATED portal router
(``gdx_dispatch/routers/payments.py``, ``/payments/methods``). This module's
unauthenticated duplicates were removed 2026-08-04.

Public (no auth):
GET  /pay/{invoice_token}          — serve the Stripe Elements payment form

Webhook entry point (dispatched from routers/stripe_webhook.py):
    handle_payment_webhook(event, db)
"""
from __future__ import annotations

import contextlib
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.customer_views import record_customer_view
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Invoice, Payment

logger = logging.getLogger(__name__)

# NOTE: this module used to declare `oauth2_scheme = OAuth2PasswordBearer(
# auto_error=False)` and give every handler `token: str | None =
# Depends(oauth2_scheme)` — a parameter that was never read. With
# auto_error=False the dependency never rejects anything, so the signatures
# LOOKED authenticated in review while enforcing nothing. It survived multiple
# audits that way. Removed 2026-08-04; if you need auth here, use a dependency
# whose result you actually consume.

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

# NOTE ON SHAPE (2026-08-04 payment-authorization hardening):
# ``invoice_token`` is the credential — it is the same unguessable token that
# addresses the /pay/{token} page, so possessing it is what authorizes the
# request. ``invoice_id`` and ``amount`` are DEPRECATED client inputs kept
# nullable for ONE release so a /pay tab opened before the deploy does not
# 422 in the middle of a payment. They are never trusted for authorization:
# the amount is always recomputed from the invoice, the PaymentIntent is
# bound to the invoice through Stripe metadata, and the ACH method is bound
# through its SetupIntent. Remove both fields (and _LEGACY_SHAPE_DEADLINE
# below) in the release after this one.
_LEGACY_SHAPE_DEADLINE = "2026-09-01"


class CreateIntentRequest(BaseModel):
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE
    amount: int | None = None  # DEPRECATED + IGNORED — server derives from balance
    # DEPRECATED + IGNORED (M4, money audit 2026-08-04). The server derived the
    # AMOUNT but passed the client's CURRENCY straight to Stripe, and the
    # webhook records `amount_received / 100` as dollars without checking it —
    # so `currency: "idr"` settled a $500 invoice for about $3. Kept on the
    # model (ignored) for one release so an open /pay tab does not 422.
    currency: str = "usd"


class ConfirmPaymentRequest(BaseModel):
    payment_intent_id: str
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE


class ACHSetupRequest(BaseModel):
    customer_email: str
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE


class ACHChargeRequest(BaseModel):
    payment_method_id: str
    # The SetupIntent that collected ``payment_method_id``. REQUIRED: it is
    # what proves the bank account was collected for THIS invoice. Without it
    # any caller could charge any saved payment method they knew the id of.
    setup_intent_id: str | None = None
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE
    amount: int | None = None  # DEPRECATED + IGNORED — server derives from balance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The only currency this application prices, charges and records in. The
# recording paths treat Stripe minor units as cents-of-a-dollar, which is only
# true for USD (JPY has no minor unit at all), so this is an invariant of the
# money model, not a default. See M4 in the 2026-08-04 money audit.
CURRENCY = "usd"


def _init_stripe() -> None:
    """Set Stripe API key from environment."""
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")


def stripe_configured() -> bool:
    """Whether online payments can actually charge (a Stripe key is set)."""
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def public_pay_url(public_token: str | None) -> str | None:
    """Absolute customer-facing /pay/{token} URL, or None when the link
    would be dead: no token, no public base URL, or Stripe unconfigured
    (the pay page renders but Stripe.js can't charge without keys)."""
    base = os.getenv("GDX_PUBLIC_BASE_URL", "").rstrip("/")
    if not (public_token and base and stripe_configured()):
        return None
    return f"{base}/pay/{public_token}"


def _stripe_extra(tenant: dict) -> dict[str, Any]:
    """Return Stripe Connect kwargs if tenant has a connected account."""
    acct = tenant.get("stripe_connect_account_id")
    return {"stripe_account": acct} if acct else {}


# ---------------------------------------------------------------------------
# Public-payment authorization (2026-08-04)
# ---------------------------------------------------------------------------
# These endpoints serve the anonymous /pay/{token} page, so there is no user to
# authenticate. Authorization is therefore structural: the caller proves which
# invoice they may touch by presenting its token, and every other money-bearing
# decision (how much, which payment method) is made server-side. Before this,
# the client supplied invoice_id, amount AND payment_method_id, so a caller
# holding any one leaked identifier could credit a payment to an unrelated
# invoice or debit an unrelated bank account.


def _resolve_public_invoice(
    db: Session,
    *,
    invoice_token: str | None,
    invoice_id: str | None,
    op: str,
    require_balance: bool = True,
) -> Invoice:
    """Resolve (and gate) the invoice a public payment request may act on.

    ``invoice_token`` is authoritative. ``invoice_id`` is the deprecated legacy
    shape (see NOTE ON SHAPE) and is accepted only until
    ``_LEGACY_SHAPE_DEADLINE``; it is safe in the interim because callers can no
    longer choose the amount, the PaymentIntent binding, or the ACH method.
    """
    invoice: Invoice | None = None
    if invoice_token:
        invoice = (
            db.query(Invoice)
            .filter(Invoice.public_token == invoice_token, Invoice.deleted_at.is_(None))
            .first()
        )
        if invoice is None:
            # Same response as an unknown invoice — never reveal whether a
            # token merely mismatched an existing invoice.
            raise HTTPException(status_code=404, detail="Invoice not found")
    elif invoice_id:
        logger.warning(
            "public_payment_legacy_shape op=%s — client sent invoice_id without "
            "invoice_token; support ends %s",
            op,
            _LEGACY_SHAPE_DEADLINE,
        )
        try:
            invoice_uuid = UUID(invoice_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="Invalid invoice_id format") from None
        invoice = db.get(Invoice, invoice_uuid)
        if invoice is None or invoice.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Invoice not found")
    else:
        raise HTTPException(status_code=422, detail="invoice_token is required")

    # An open /pay tab can outlive the invoice — voided, or a deposit settled
    # when the final invoice superseded it. Don't move money nothing is owed on.
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="This invoice has been cancelled.")
    # §11 rail (2026-08-08 audit): a DRAFT was payable here — including a
    # machine-priced closeout autodraft nobody reviewed, whose token is
    # minted at creation. An un-issued invoice must not take money; 404 (not
    # 409) so a leaked pre-issue token reveals nothing.
    if str(invoice.status or "").lower() == "draft":
        raise HTTPException(status_code=404, detail="Invoice not found")
    # `require_balance=False` for RECORDING operations (confirm). The signed
    # webhook usually beats the browser's confirm call, so by the time confirm
    # runs the balance is legitimately zero — rejecting it there would 409 the
    # customer's own success callback on every fast payment.
    if require_balance and float(invoice.balance_due or 0) <= 0:
        raise HTTPException(status_code=409, detail="This invoice has no balance due.")
    return invoice


def _amount_cents(invoice: Invoice) -> int:
    """The only amount this module will ever charge: what is actually owed.

    Never the client's number, and never ``invoice.total`` — a partially paid,
    credit-memo'd or deposit-applied invoice owes less than its total, and
    charging the total overcharges a real customer.
    """
    return int(round(float(invoice.balance_due or 0) * 100))


def _idempotency_key(invoice: Invoice, amount_cents: int, method: str) -> str:
    """Stripe idempotency key for a public payment attempt.

    MUST vary with amount AND method. Stripe rejects a reused key whose
    parameters differ (for 24h), so:
      * ``gdx-pi-{invoice_id}`` alone wedges the customer out of paying as soon
        as the balance changes between attempts (partial payment lands, or an
        earlier attempt sent the old client-supplied amount);
      * omitting the method wedges the customer who tries card, fails, then
        switches to ACH for the same balance — different params, same key.
    """
    return f"gdx-pi-{invoice.id}-{method}-{amount_cents}"


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
                # M14 (money audit 2026-08-04): must exclude VOIDED rows. A
                # wrongly-reversed payment (late charge.failed on a retried
                # intent) left a voided row here, and this check then blocked
                # the redelivered `succeeded` from re-recording it — the
                # invoice stayed open with the money collected.
                Payment.voided_at.is_(None),
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
    try:
        db.flush()
    except IntegrityError:
        # M2: the existence check above is optimistic — /confirm and the
        # webhook race by design ("the signed webhook usually beats the
        # browser's confirm call"), and both saw no row. Migration 056's
        # partial unique index on (invoice_id, reference) is what actually
        # decides it; losing the race means the payment IS recorded, so this
        # is the idempotent no-op it always claimed to be.
        db.rollback()
        logger.info(
            "payment_already_recorded_by_concurrent_writer invoice=%s ref=%s",
            invoice.id, external_ref,
        )
        return
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
) -> dict:
    """Create a Stripe PaymentIntent for the invoice the caller's token names.

    The amount is the invoice's balance due — the client's ``amount`` is
    ignored. ``metadata.invoice_id`` binds the intent to this invoice so
    ``confirm`` can refuse to credit it anywhere else.
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db, invoice_token=body.invoice_token, invoice_id=body.invoice_id, op="create-intent"
    )
    amount_cents = _amount_cents(invoice)
    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        pi = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=CURRENCY,  # M4: never body.currency
            metadata={
                "invoice_id": str(invoice.id),
                "tenant_id": str(tenant.get("id", "")),
            },
            idempotency_key=_idempotency_key(invoice, amount_cents, "card"),
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe create_intent error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {
        "client_secret": pi.client_secret,
        "payment_intent_id": pi.id,
        # What will actually be charged. The pay page renders the balance
        # server-side and does not read this back — it is here so any API
        # consumer (and anyone debugging a disputed charge) can see the
        # server's figure rather than inferring it.
        "amount": amount_cents,
    }


# ---------------------------------------------------------------------------
# POST /api/payments/confirm
# ---------------------------------------------------------------------------

@router.post("/confirm")
def confirm_payment(
    body: ConfirmPaymentRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Confirm payment after Stripe.js reports success.

    The PaymentIntent must carry ``metadata.invoice_id`` matching the invoice
    the caller's token resolves to. Without that check a succeeded intent could
    be replayed against any invoice — idempotency is keyed on
    ``(invoice_id, reference)``, so one real payment could settle a different
    invoice as well as its own.
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db,
        invoice_token=body.invoice_token,
        invoice_id=body.invoice_id,
        op="confirm",
        require_balance=False,
    )

    try:
        pi = stripe.PaymentIntent.retrieve(body.payment_intent_id)
    except stripe.StripeError as exc:
        logger.error("Stripe retrieve error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    intent_invoice_id = str((getattr(pi, "metadata", None) or {}).get("invoice_id") or "")
    if intent_invoice_id != str(invoice.id):
        logger.warning(
            "payment_confirm_invoice_mismatch pi=%s intent_invoice=%s requested_invoice=%s",
            body.payment_intent_id,
            intent_invoice_id or "<none>",
            invoice.id,
        )
        raise HTTPException(
            status_code=409,
            detail="This payment does not belong to this invoice.",
        )

    if pi.status == "succeeded":
        _mark_invoice_paid(
            invoice, db, external_ref=pi.id, method="card", amount=(pi.amount or 0) / 100.0
        )

    return {"status": pi.status, "invoice_id": str(invoice.id)}


# ---------------------------------------------------------------------------
# POST /api/payments/ach/setup
# ---------------------------------------------------------------------------

@router.post("/ach/setup")
def ach_setup(
    body: ACHSetupRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a SetupIntent so Stripe.js can collect ACH bank account details.

    Scoped to an invoice: previously this took only an email, so anyone could
    mint unlimited SetupIntents. The ``metadata.invoice_id`` stamped here is
    what ``ach/charge`` later checks to prove the collected bank account was
    gathered for THIS invoice.
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db, invoice_token=body.invoice_token, invoice_id=body.invoice_id, op="ach-setup"
    )
    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        si = stripe.SetupIntent.create(
            payment_method_types=["us_bank_account"],
            metadata={
                "email": body.customer_email,
                "invoice_id": str(invoice.id),
                "tenant_id": str(tenant.get("id", "")),
            },
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe ach_setup error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {"client_secret": si.client_secret, "setup_intent_id": si.id}


# ---------------------------------------------------------------------------
# POST /api/payments/ach/charge
# ---------------------------------------------------------------------------

@router.post("/ach/charge")
def ach_charge(
    body: ACHChargeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Charge the bank account collected for this invoice.

    Creates a PaymentIntent with ``confirm=True`` so the charge is initiated
    immediately. ACH payments are typically pending for 1-2 business days.

    ``setup_intent_id`` is required and must be a SetupIntent minted by
    ``ach/setup`` for THIS invoice that collected THIS payment method. That
    chain is the authorization: without it, knowing any ``pm_`` id was enough
    to debit an unrelated person's bank account (an unauthorized ACH debit,
    which is a NACHA violation regardless of where the money lands).
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db, invoice_token=body.invoice_token, invoice_id=body.invoice_id, op="ach-charge"
    )

    if not body.setup_intent_id:
        # Fail closed. An ACH tab opened before this deploy has no
        # setup_intent_id; retrying from a fresh page costs the customer one
        # reload, whereas charging an unverifiable bank account is a debit we
        # cannot justify.
        raise HTTPException(
            status_code=409,
            detail="This payment session is out of date. Please refresh the page and try again.",
        )

    try:
        si = stripe.SetupIntent.retrieve(body.setup_intent_id)
    except stripe.StripeError as exc:
        logger.error("Stripe ach_charge setup-intent retrieve error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    si_invoice_id = str((getattr(si, "metadata", None) or {}).get("invoice_id") or "")
    si_payment_method = str(getattr(si, "payment_method", "") or "")
    if si_invoice_id != str(invoice.id) or si_payment_method != body.payment_method_id:
        logger.warning(
            "ach_charge_binding_mismatch si=%s si_invoice=%s si_pm=%s "
            "requested_invoice=%s requested_pm=%s",
            body.setup_intent_id,
            si_invoice_id or "<none>",
            si_payment_method or "<none>",
            invoice.id,
            body.payment_method_id,
        )
        raise HTTPException(
            status_code=409,
            detail="This bank account was not set up for this invoice.",
        )

    amount_cents = _amount_cents(invoice)
    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        pi = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            payment_method=body.payment_method_id,
            payment_method_types=["us_bank_account"],
            confirm=True,
            metadata={
                "invoice_id": str(invoice.id),
                "tenant_id": str(tenant.get("id", "")),
            },
            idempotency_key=_idempotency_key(invoice, amount_cents, "ach"),
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe ach_charge error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    # ACH payments may be processing (not yet succeeded); mark partial if needed
    if pi.status == "succeeded":
        _mark_invoice_paid(invoice, db, external_ref=pi.id, method="ach", amount=(pi.amount or 0) / 100.0)

    return {"status": pi.status, "payment_intent_id": pi.id}


# NOTE: ``GET /api/payments/methods`` and ``DELETE /api/payments/methods/{pm_id}``
# were removed 2026-08-04. They were unauthenticated (leaking card brand/last4
# and bank details for any Stripe customer id, and detaching any payment
# method) and had no production caller — the payment-methods UI has always used
# the portal router's authenticated ``/payments/methods``.


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
    # §11 rail (2026-08-08 audit): the form rendered for DRAFTS — an
    # unreviewed autodraft presented a full Stripe payment page, and the
    # render also logged invoice_viewed_by_customer as if it were a
    # delivered invoice. Un-issued = not found (never reveal pre-issue
    # invoices to a leaked token).
    if str(invoice.status or "").lower() == "draft":
        raise HTTPException(status_code=404, detail="Invoice not found or expired")

    # The customer clicked the link we emailed them. Never blocks the page.
    record_customer_view(
        db,
        action="invoice_viewed_by_customer",
        entity_type="invoice",
        entity_id=invoice.id,
        tenant_id=getattr(invoice, "company_id", None),
        request=request,
        sent_at=getattr(invoice, "sent_at", None),
        details={"invoice_number": getattr(invoice, "invoice_number", None)},
    )

    return templates.TemplateResponse(
        request,
        "payment_form.html",
        {
            "invoice": invoice,
            "stripe_publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
            # The photos the office attached to THIS invoice (Doug 2026-08-12:
            # photos are customer-facing). They already ride the PDF; showing
            # them on the page the customer actually opens is the same
            # disclosure, one click earlier — "here is the work you're paying
            # for". Strictly the attached set, never the job's whole roll.
            "job_photos": _invoice_public_photos(invoice, db),
        },
    )


# How many attached photos the pay page will inline. The picker allows up to
# 20 per invoice; twenty full photos inlined would be megabytes on a phone,
# and nobody scrolls past the first few above a card form. The PDF still
# carries the complete set.
_PAY_PAGE_MAX_PHOTOS = 6


def _invoice_public_photos(invoice: Any, db: Session) -> list[dict[str, Any]]:
    """The attached photos for the public pay page, as {src, label} rows.

    Same selection the PDF prints (invoice.attached_photo_ids, in pick order)
    resolved through the same helper, so the page and the attachment can never
    show the customer different pictures.

    Inlined as data: URIs rather than served from a new public route. The pay
    page is anonymous by design, and the ungated-route baseline is a ratchet to
    work DOWN — adding an endpoint to show a picture is the wrong trade when
    the bytes can ride inside the page the token already unlocked.
    """
    import json as _json_mod

    from gdx_dispatch.core.job_photos import photo_data_uri, resolve_photo_file
    from gdx_dispatch.models.tenant_models import JobPhoto

    raw = getattr(invoice, "attached_photo_ids", None)
    if not raw or getattr(invoice, "job_id", None) is None:
        return []
    try:
        ids = [str(i) for i in _json_mod.loads(raw) if i]
    except (ValueError, TypeError):
        return []
    if not ids:
        return []
    uuids = []
    for i in ids:
        with contextlib.suppress(ValueError, AttributeError):
            uuids.append(_uuid.UUID(i))
    if not uuids:
        return []
    rows = db.execute(
        select(JobPhoto).where(
            JobPhoto.id.in_(uuids),
            JobPhoto.job_id == invoice.job_id,
            # The same share gate the portal uses (migration 063). Attaching a
            # photo to an invoice SETS this flag, so the normal path needs no
            # second decision — but un-sharing a photo afterwards has to pull
            # it off the customer's page, or "internal" would mean internal
            # everywhere except the bill.
            JobPhoto.customer_visible.is_(True),
            JobPhoto.deleted_at.is_(None),
        )
    ).scalars().all()
    by_id = {str(p.id): p for p in rows}
    out: list[dict[str, Any]] = []
    for pid in ids:  # pick order is display order
        if len(out) >= _PAY_PAGE_MAX_PHOTOS:
            break
        photo = by_id.get(pid)
        if photo is None:
            continue
        resolved = resolve_photo_file(db, photo)
        if resolved is None:
            continue
        src = photo_data_uri(*resolved)
        if src is None:
            continue
        out.append({
            "src": src,
            "label": (photo.caption or "").strip() or (photo.kind or "").strip().title(),
        })
    return out




# ---------------------------------------------------------------------------
# Webhook helper (called from gdx_dispatch/routers/stripe_webhook.py)
# ---------------------------------------------------------------------------

def _reverse_recorded_payment(db: Session, reference: str, reason: str) -> dict:
    """Void the Payment row recorded for ``reference`` and re-open the invoice.

    Money that arrived can leave again: an ACH debit can be returned days later
    (R01 insufficient funds, R10 unauthorized), a card charge can be refunded,
    a customer can dispute. Recording only the arrival is how books drift —
    the invoice reads paid, dunning stops chasing, and the cash is gone.

    Voiding (rather than deleting) keeps the history; ``_recalculate_invoice``
    excludes voided payments, so the balance comes back and the status flips
    off "paid" on its own.
    """
    from sqlalchemy import select as _select

    from gdx_dispatch.routers.invoices import _recalculate_invoice

    if not reference:
        return {"status": "no_reference"}

    payment = db.scalars(
        _select(Payment).where(Payment.reference == reference, Payment.voided_at.is_(None))
    ).first()
    if payment is None:
        # Nothing recorded for this charge (e.g. the reversal arrived before we
        # ever saw the success). Not an error.
        return {"status": "no_payment_to_reverse", "reference": reference}

    payment.voided_at = datetime.now(timezone.utc)
    # Flush explicitly: _recalculate_invoice SUMs non-voided payments with a
    # SELECT, and on an autoflush=False session the pending void would not be
    # visible to it — the balance would come back unchanged and the invoice
    # would stay "paid" with the money gone.
    db.flush()
    invoice = db.get(Invoice, payment.invoice_id)
    if invoice is not None:
        _recalculate_invoice(invoice, db)
        # _recalculate_invoice only ever flips an invoice TO "paid"; there is
        # no reverse edge, so an invoice whose payment just got voided keeps
        # reading "paid" with a positive balance. Un-pay it explicitly, back
        # to "sent" (it was, in fact, sent to the customer) so it re-enters
        # aging and dunning.
        if invoice.status == "paid" and float(invoice.balance_due or 0) > 0:
            from gdx_dispatch.modules.ledger.service import transition_invoice_status

            transition_invoice_status(db, invoice, "sent", actor="stripe-webhook")
            invoice.paid_at = None
    db.commit()
    logger.warning(
        "payment_reversed reference=%s invoice=%s reason=%s — invoice re-opened",
        reference, payment.invoice_id, reason,
    )
    return {"status": "reversed", "invoice_id": str(payment.invoice_id), "reason": reason}


def handle_payment_webhook(event: dict, db: Session) -> dict:
    """Process Stripe payment events from the webhook router.

    Raises on unexpected failure — the router turns that into a 500 so Stripe
    retries with backoff. Swallowing the error and returning 200 (the pre-
    2026-08-04 behavior) tells Stripe the event was handled and it is never
    redelivered, so a transient DB failure silently loses a payment.
    """
    event_type: str = event.get("type", "")
    data: dict = event.get("data", {}).get("object", {})

    if event_type == "payment_intent.succeeded":
        invoice_id: str = (data.get("metadata") or {}).get("invoice_id", "")
        if not invoice_id:
            return {"status": "no_invoice_id"}
        # M4: `amount_received` is in the intent's own minor unit. Recording it
        # as dollars is only correct for USD, so refuse anything else rather
        # than booking a foreign-currency charge at face value. Loud, because
        # a mismatch here means money moved that we cannot record truthfully.
        event_currency = str(data.get("currency") or CURRENCY).lower()
        if event_currency != CURRENCY:
            logger.error(
                "payment_currency_mismatch invoice=%s currency=%s intent=%s — "
                "NOT recorded; refund at the processor",
                invoice_id, event_currency, data.get("id"),
            )
            return {"status": "currency_mismatch", "currency": event_currency}
        # ACH settles asynchronously (1-2 business days), so this webhook —
        # not /confirm — is how most bank payments get recorded. Label the
        # method from the intent instead of assuming "card", or every ACH
        # payment lands in the books as a card payment.
        pm_types = data.get("payment_method_types") or []
        method = "ach" if "us_bank_account" in pm_types else "card"
        invoice = db.get(Invoice, UUID(invoice_id))
        if invoice is None or invoice.deleted_at is not None:
            return {"status": "no_invoice", "invoice_id": invoice_id}
        # NOTE: no `status != "paid"` guard. _mark_invoice_paid is idempotent
        # on (invoice_id, reference), so a redelivery is a no-op, but a SECOND
        # genuine payment on an already-paid invoice must still be recorded —
        # otherwise the customer's money sits at Stripe with no Payment row
        # and nothing to refund against.
        _mark_invoice_paid(
            invoice, db,
            external_ref=data.get("id"),
            method=method,
            amount=(data.get("amount_received") or 0) / 100.0,
        )
        logger.info("Invoice %s marked paid via webhook", invoice_id)
        # Receipt email placeholder — wire up notification service here
        # send_receipt_email(invoice)
        return {"status": "paid", "invoice_id": invoice_id}

    # Money leaving again. `charge.*` events carry the PaymentIntent id in
    # `payment_intent`; that is the same value stored as Payment.reference.
    if event_type in ("charge.refunded", "charge.dispute.created", "charge.failed"):
        return _reverse_recorded_payment(
            db, str(data.get("payment_intent") or ""), event_type
        )

    if event_type == "payment_intent.payment_failed":
        invoice_id = (data.get("metadata") or {}).get("invoice_id", "")
        failure_msg = (data.get("last_payment_error") or {}).get("message", "unknown")
        logger.warning(
            "PaymentIntent failed for invoice %s: %s",
            invoice_id,
            failure_msg,
        )
        # An ACH return can arrive as payment_failed AFTER a succeeded event,
        # so reverse anything already recorded for this intent.
        reversal = _reverse_recorded_payment(db, str(data.get("id") or ""), event_type)
        # Tenant notification placeholder — wire up notification service here
        # notify_tenant_payment_failed(invoice_id, failure_msg)
        return {
            "status": "failed",
            "invoice_id": invoice_id,
            "reason": failure_msg,
            "reversal": reversal["status"],
        }

    return {"status": "ignored"}
