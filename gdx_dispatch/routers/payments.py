"""
gdx_dispatch/routers/payments.py — Payment processing routes for the customer portal.

Provides endpoints for creating payment intents, saving payment methods,
ACH bank account setup, and charging saved methods. All routes require
customer portal authentication via the portal session cookie.
"""
from __future__ import annotations

import contextlib
import logging
import os
import time as _time
from typing import Any

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor
from gdx_dispatch.core.database import get_db
# One currency constant for every money path on this router (M4).
from gdx_dispatch.core.payments import CURRENCY
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.stripe_payments import (
    charge_saved_method,
    create_ach_verification,
    create_payment_intent,
    create_setup_intent,
    list_payment_methods,
)
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.routers.portal import PortalPrincipal
from gdx_dispatch.routers.portal import get_current_portal_customer as _get_portal_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"], dependencies=[Depends(require_module("invoices"))])


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class CreateIntentRequest(BaseModel):
    """M13: `amount_cents` and `currency` are now ADVISORY and ignored.

    The server derives the amount from the invoice named in
    `metadata.invoice_id` and hardcodes the currency. They are kept on the
    model rather than removed so existing portal clients keep sending a valid
    body instead of 422-ing mid-payment, and so the handler can record the
    disagreement when a client asks for an amount the invoice does not owe.
    Nothing here reaches Stripe.
    """

    # amount_cents max = $1M (100_000_000 cents) — rejects obvious nonsense.
    amount_cents: int = Field(ge=1, le=100_000_000)
    currency: str = Field(default="usd", min_length=3, max_length=3, pattern=r"^[a-z]{3}$")
    # Must carry `invoice_id`. Every other key is dropped.
    metadata: dict[str, Any] | None = None


class ChargeRequest(BaseModel):
    amount_cents: int = Field(ge=1, le=100_000_000)
    currency: str = Field(default="usd", min_length=3, max_length=3, pattern=r"^[a-z]{3}$")
    metadata: dict[str, Any] | None = None
    # Idempotency key for the charge. Prefer the Idempotency-Key header; this
    # body field is a fallback. Forwarded to Stripe so a retried/double-submitted
    # request collapses into a single charge.
    idempotency_key: str | None = None


class ACHSetupRequest(BaseModel):
    # US ACH routing numbers are exactly 9 digits; account numbers are 4–17
    # digits. Bank names bounded to 120 chars (longer than any real US bank).
    bank_name: str = Field(min_length=1, max_length=120)
    routing: str = Field(min_length=9, max_length=9, pattern=r"^\d{9}$")
    account: str = Field(min_length=4, max_length=17, pattern=r"^\d{4,17}$")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _current_portal_user(
    principal: PortalPrincipal = Depends(_get_portal_principal),
    db: Session = Depends(get_db),
) -> CustomerUser:
    """Require an authenticated customer portal session (JWT bearer).

    ADR-018 follow-up: this used to trust the raw `customer_portal_user_id`
    cookie — an unsigned user id anyone could forge — and the only route that
    ever *set* that cookie was dead code. Now rides the same JWT the portal
    SPA holds, so a forged cookie buys nothing.
    """
    user = db.execute(
        select(CustomerUser).where(
            CustomerUser.id == principal.user_id,
            CustomerUser.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Customer portal authentication required")
    return user


def _require_own_unpaid_invoice(db: Session, user: CustomerUser, invoice_ref: Any) -> Any:
    """Resolve an invoice the portal user is actually allowed to pay.

    Enforces the authorization the charge path was missing: the invoice must
    exist, belong to THIS portal user's customer, not be void, and still owe
    money. Returns the Invoice row so the caller can derive the amount from it
    rather than trusting a client-supplied figure.
    """
    from uuid import UUID as _UUID  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import Invoice  # noqa: PLC0415

    if not invoice_ref:
        raise HTTPException(status_code=422, detail="metadata.invoice_id is required")
    try:
        invoice_uuid = _UUID(str(invoice_ref))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid invoice_id") from None

    invoice = db.get(Invoice, invoice_uuid)
    if invoice is None or invoice.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if str(invoice.customer_id) != str(user.customer_id):
        # Same 404 as a missing invoice — don't confirm that someone else's
        # invoice id is real.
        logger.warning(
            "portal_charge_invoice_ownership_denied user=%s customer=%s invoice=%s",
            getattr(user, "id", "?"), getattr(user, "customer_id", "?"), invoice_uuid,
        )
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="This invoice has been cancelled.")
    # §11 rail. `core/payments.py:_resolve_public_invoice` has refused DRAFTS
    # since the 2026-08-08 audit — "a machine-priced closeout autodraft nobody
    # reviewed" must not take money — but THIS helper never learned it, so
    # both portal endpoints it guards were 15 days behind the resolver next
    # door. Found by an adversarial review of the /intent fix, which had
    # measured parity against this helper and called it "the full treatment".
    #
    # 404, not 409: an un-issued invoice should not be confirmed to exist.
    if str(invoice.status or "").lower() == "draft":
        raise HTTPException(status_code=404, detail="Invoice not found")
    if float(invoice.balance_due or 0) <= 0:
        raise HTTPException(status_code=409, detail="This invoice has no balance due.")
    return invoice


def _require_stripe_customer(user: CustomerUser) -> str:
    """Return the Stripe customer ID or raise 400 if not set."""
    stripe_cid = getattr(user, "stripe_customer_id", None)
    if not stripe_cid:
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer account linked to this portal user. Contact support.",
        )
    return stripe_cid


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/intent", response_model=None)
def payment_intent(
    body: CreateIntentRequest,
    request: Request,
    user: CustomerUser = Depends(_current_portal_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a PaymentIntent for an immediate one-time charge.

    Money audit **M13**. `charge_method` on this router got the full treatment
    on 2026-08-04 — ownership check, server-derived amount, hardcoded currency.
    Its sibling here did not: `amount_cents` and `currency` came from the body
    and `metadata` was forwarded verbatim, with no invoice reference required.

    That mattered because `core/payments.py` records the resulting payment
    against `metadata.invoice_id`: an authenticated portal user could mint an
    intent carrying **any** invoice UUID and have their payment land on
    another customer's bill — authentication enforced, authorization absent.
    The same shape as the charge-path hole, on the sibling nobody closed.

    **Latent, never exploitable.** `_require_stripe_customer` runs first and
    `CustomerUser` has no `stripe_customer_id` column, so every real caller
    400s before reaching any of this, and prod has zero `payment_intent` audit
    rows. Nothing in the repo calls this endpoint either — the live customer
    pay path is the token-scoped one in `core/payments.py`. Hardened because
    the day that column is added the hole opens silently, not because anyone
    walked through it.

    Now identical to `charge_method`: the invoice must exist, belong to THIS
    portal user's customer, not be void and still owe money; the amount is that
    invoice's balance due; the currency is fixed; and `metadata.invoice_id` is
    overwritten with the resolved id so a forged one cannot survive.
    """
    stripe_cid = _require_stripe_customer(user)

    invoice = _require_own_unpaid_invoice(db, user, (body.metadata or {}).get("invoice_id"))
    amount_cents = int(round(float(invoice.balance_due or 0) * 100))
    # Whitelist, not passthrough. Stripe metadata rides into the webhook and
    # some of it is read back as fact; an arbitrary client-controlled dict on a
    # money object is a channel nobody audits.
    metadata = {"invoice_id": str(invoice.id)}
    try:
        intent = create_payment_intent(
            amount_cents=amount_cents,
            currency=CURRENCY,
            customer_id=stripe_cid,
            metadata=metadata,
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe error creating PaymentIntent for customer %s: %s", stripe_cid, exc)
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc
    # The audit write used to read `locals().get('db')` on a handler that took
    # no `db` parameter, so it was ALWAYS None and this endpoint has never
    # written a single row — a money path with a silent no-op where its trail
    # should be. It also recorded `entity_id=""` and `details={}`, which would
    # have said nothing even had it fired.
    try:
        log_audit_event_sync(
            db,
            tenant_id=str((getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id") or ""),
            user_id=resolve_audit_actor(user, request),
            action="payment_intent",
            entity_type="invoice",
            entity_id=str(invoice.id),
            details={
                "invoice_number": invoice.invoice_number,
                "amount_cents": amount_cents,
                "payment_intent_id": intent.id,
                # What the client ASKED for, when it disagreed with the
                # invoice. A mismatch is the signal that something is probing.
                "client_amount_cents": (
                    body.amount_cents if body.amount_cents != amount_cents else None
                ),
            },
            request=request,
        )
        db.commit()
    except Exception:
        # Never 500 a customer whose card Stripe has already accepted. Roll
        # back so the session is usable rather than poisoned, and shout.
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("payment_intent_audit_failed")
    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
    }


@router.post("/setup", response_model=None)
def setup_intent(
    request: Request,
    user: CustomerUser = Depends(_current_portal_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a SetupIntent to save a payment method without charging."""
    stripe_cid = _require_stripe_customer(user)
    try:
        intent = create_setup_intent(customer_id=stripe_cid)
    except stripe.error.StripeError as exc:
        logger.error("Stripe error creating SetupIntent for customer %s: %s", stripe_cid, exc)
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc
    # Was `locals().get('db')` on a handler that took no `db` — always None,
    # so this never wrote a row; `entity_id=""` + `details={}` would have said
    # nothing had it fired. Threaded through, and given a subject.
    try:
        log_audit_event_sync(
            db,
            tenant_id=str((getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id") or ""),
            user_id=resolve_audit_actor(user, request),
            action="setup_intent",
            entity_type="setup_intent",
            entity_id=str(getattr(user, "id", "") or ""),
            details={"setup_intent_id": intent.id, "stripe_customer_id": stripe_cid},
            request=request,
        )
        db.commit()
    except Exception:
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("setup_intent_audit_failed")
    return {
        "client_secret": intent.client_secret,
        "setup_intent_id": intent.id,
    }


@router.get("/methods", response_model=None)
def get_payment_methods(
    user: CustomerUser = Depends(_current_portal_user),
) -> list[dict[str, Any]]:
    """List all saved payment methods (cards and bank accounts) for the customer."""
    stripe_cid = _require_stripe_customer(user)
    results: list[dict[str, Any]] = []

    try:
        cards = list_payment_methods(customer_id=stripe_cid, pm_type="card")
        for pm in cards:
            card = getattr(pm, "card", None)
            results.append(
                {
                    "id": pm.id,
                    "type": "card",
                    "brand": getattr(card, "brand", None) if card else None,
                    "last4": getattr(card, "last4", None) if card else None,
                    "exp_month": getattr(card, "exp_month", None) if card else None,
                    "exp_year": getattr(card, "exp_year", None) if card else None,
                }
            )
    except stripe.error.StripeError as exc:
        logger.error("Stripe error listing cards for customer %s: %s", stripe_cid, exc)
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc

    try:
        bank_accounts = list_payment_methods(customer_id=stripe_cid, pm_type="us_bank_account")
        for pm in bank_accounts:
            ba = getattr(pm, "us_bank_account", None)
            results.append(
                {
                    "id": pm.id,
                    "type": "bank_account",
                    "bank_name": getattr(ba, "bank_name", None) if ba else None,
                    "last4": getattr(ba, "last4", None) if ba else None,
                    "status": getattr(ba, "status", None) if ba else None,
                }
            )
    except stripe.error.StripeError:
        # Bank account listing is optional — skip on error
        logger.exception("stripe_bank_account_list_failed")

    return results


@router.post("/methods/{method_id}/charge", response_model=None)
def charge_method(
    method_id: str,
    body: ChargeRequest,
    user: CustomerUser = Depends(_current_portal_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Charge a previously saved payment method off-session.

    The invoice named in ``metadata.invoice_id`` must belong to the logged-in
    portal user, and the amount charged is that invoice's balance due.

    Before 2026-08-04 both came from the request body with no ownership check:
    authentication was enforced but authorization was not, so any portal
    customer could charge their own saved card and have the payment recorded
    against ANY invoice in the system — including another customer's.
    """
    stripe_cid = _require_stripe_customer(user)

    invoice = _require_own_unpaid_invoice(db, user, (body.metadata or {}).get("invoice_id"))
    amount_cents = int(round(float(invoice.balance_due or 0) * 100))
    # Whitelist, not merge. `{**body.metadata}` forwarded an arbitrary
    # client-controlled dict onto a Stripe money object that rides into the
    # webhook — a channel nobody audits. Only the resolved invoice id survives.
    metadata = {"invoice_id": str(invoice.id)}

    # Prevent double-charge on retry/double-click: forward an idempotency key to
    # Stripe (Idempotency-Key header wins; ChargeRequest.idempotency_key next).
    # M17.2: both were OPTIONAL, so a caller that sent neither got no
    # protection at all — a double-click minted two distinct intents and two
    # full-balance charges.
    #
    # The fallback is TIME-BUCKETED, unlike the public path's static shape,
    # because this create runs with `confirm=True` and Stripe replays the
    # FIRST request's saved response for 24h (adversarial review): a static
    # key would replay a cached DECLINE at the customer who fixed their funds,
    # and — worse — replay a stale SUCCESS after a void restored the balance,
    # recording a phantom payment with no money moved. A 30s bucket collapses
    # the double-click it exists for; a deliberate retry lands in a fresh
    # bucket and charges for real. Residual: a replay within the same 30s of a
    # void-then-retry — accepted, and this endpoint remains latent anyway
    # (CustomerUser has no stripe_customer_id). Suffixed with the payment
    # method id: charging a DIFFERENT saved card is a legitimate second
    # attempt, not a retry to collapse.
    _idem = (
        idempotency_key
        or body.idempotency_key
        or (
            f"gdx-pi-{invoice.id}-portal-{method_id}-{amount_cents}"
            f"-b{int(_time.time() // 30)}"
        )
    )
    try:
        intent = charge_saved_method(
            customer_id=stripe_cid,
            payment_method_id=method_id,
            amount_cents=amount_cents,
            # Hardcoded, like every other money path here (M4). `body.currency`
            # was still being honoured on this endpoint even after the
            # 2026-08-04 hardening — the "gold standard" its sibling was
            # measured against was itself two changes short.
            currency=CURRENCY,
            metadata=metadata,
            idempotency_key=_idem,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "Stripe error charging method %s for customer %s: %s",
            method_id,
            stripe_cid,
            exc,
        )
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc

    # GL S6 (bug #1, spec §5.3 Stripe consolidation): the portal charge was
    # the last money path moving processor funds with NO Payment row. Route
    # through the one recording function — idempotent on the intent id.
    if intent.status == "succeeded":
        try:
            from gdx_dispatch.core.payments import _mark_invoice_paid

            _mark_invoice_paid(
                invoice, db,
                external_ref=intent.id,
                method="card",
                # M17.3 sibling: same asked-vs-moved distinction as /confirm.
                amount=(getattr(intent, "amount_received", None) or intent.amount or 0) / 100.0,
                source="portal-charge-method",
            )
        except Exception:
            # The charge SUCCEEDED at Stripe — never 500 the customer for a
            # local recording failure; the log line is the reconciliation cue.
            logger.exception("portal_charge_payment_recording_failed intent=%s", intent.id)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="charge_method",
                entity_type="charge_method",
                entity_id=str(method_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            logger.exception('charge_method_audit_failed')
    return {
        "payment_intent_id": intent.id,
        "status": intent.status,
        "amount": intent.amount,
        "currency": intent.currency,
    }


@router.post("/ach/setup", response_model=None)
def ach_setup(
    body: ACHSetupRequest,
    request: Request,
    user: CustomerUser = Depends(_current_portal_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Initiate ACH bank account setup via micro-deposit verification."""
    stripe_cid = _require_stripe_customer(user)
    try:
        source = create_ach_verification(
            bank_name=body.bank_name,
            routing=body.routing,
            account=body.account,
            customer_id=stripe_cid,
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe error setting up ACH for customer %s: %s", stripe_cid, exc)
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc
    # Was `locals().get('db')` on a handler that took no `db` — always None,
    # so this never wrote a row; `entity_id=""` + `details={}` would have said
    # nothing had it fired. Threaded through, and given a subject.
    try:
        log_audit_event_sync(
            db,
            tenant_id=str((getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id") or ""),
            user_id=resolve_audit_actor(user, request),
            action="ach_setup",
            entity_type="ach_setup",
            entity_id=str(getattr(user, "id", "") or ""),
            details={"bank_name": body.bank_name, "stripe_customer_id": stripe_cid},
            request=request,
        )
        db.commit()
    except Exception:
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("ach_setup_audit_failed")
    return {
        "source_id": source.id,
        "last4": getattr(source, "last4", None),
        "status": getattr(source, "status", None),
        "bank_name": getattr(source, "bank_name", body.bank_name),
    }


@router.delete("/methods/{method_id}", response_model=None)
def remove_payment_method(
    method_id: str,
    request: Request,
    user: CustomerUser = Depends(_current_portal_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Detach (remove) a saved payment method from the customer account."""
    stripe_cid = _require_stripe_customer(user)
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    try:
        stripe.PaymentMethod.detach(method_id)
    except stripe.error.StripeError as exc:
        logger.error(
            "Stripe error detaching method %s for customer %s: %s",
            method_id,
            stripe_cid,
            exc,
        )
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc
    logger.info("Detached PaymentMethod %s from customer %s", method_id, stripe_cid)
    # Was `locals().get('db')` on a handler that took no `db` — always None,
    # so this never wrote a row; `entity_id=""` + `details={}` would have said
    # nothing had it fired. Threaded through, and given a subject.
    try:
        log_audit_event_sync(
            db,
            tenant_id=str((getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id") or ""),
            user_id=resolve_audit_actor(user, request),
            action="remove_payment_method",
            entity_type="payment_method",
            entity_id=method_id,
            details={"stripe_customer_id": stripe_cid},
            request=request,
        )
        db.commit()
    except Exception:
        with contextlib.suppress(Exception):
            db.rollback()
        logger.exception("remove_payment_method_audit_failed")
    return {"status": "removed", "payment_method_id": method_id}
