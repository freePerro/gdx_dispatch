"""
gdx_dispatch/routers/payments.py — Payment processing routes for the customer portal.

Provides endpoints for creating payment intents, saving payment methods,
ACH bank account setup, and charging saved methods. All routes require
customer portal authentication via the portal session cookie.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.stripe_payments import (
    charge_saved_method,
    create_ach_verification,
    create_payment_intent,
    create_setup_intent,
    list_payment_methods,
)
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.routers.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"], dependencies=[Depends(require_module("invoices"))])


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class CreateIntentRequest(BaseModel):
    # amount_cents max = $1M (100_000_000 cents) — rejects obvious nonsense.
    # currency is ISO 4217 three-letter code.
    amount_cents: int = Field(ge=1, le=100_000_000)
    currency: str = Field(default="usd", min_length=3, max_length=3, pattern=r"^[a-z]{3}$")
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

def _current_portal_user(request: Request, db: Session = Depends(get_db)) -> CustomerUser:
    """Require an authenticated customer portal session."""
    user_id = request.cookies.get("customer_portal_user_id")
    user = (
        db.execute(
            select(CustomerUser).where(
                CustomerUser.id == user_id,
                CustomerUser.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if user_id
        else None
    )
    if not user:
        raise HTTPException(status_code=401, detail="Customer portal authentication required")
    return user


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
    user: CustomerUser = Depends(_current_portal_user),
) -> dict[str, Any]:
    """Create a PaymentIntent for an immediate one-time charge."""
    stripe_cid = _require_stripe_customer(user)
    try:
        intent = create_payment_intent(
            amount_cents=body.amount_cents,
            currency=body.currency,
            customer_id=stripe_cid,
            metadata=body.metadata,
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe error creating PaymentIntent for customer %s: %s", stripe_cid, exc)
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="payment_intent",
                entity_type="payment_intent",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            logger.exception('payment_intent_audit_failed')
    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
    }


@router.post("/setup", response_model=None)
def setup_intent(
    user: CustomerUser = Depends(_current_portal_user),
) -> dict[str, Any]:
    """Create a SetupIntent to save a payment method without charging."""
    stripe_cid = _require_stripe_customer(user)
    try:
        intent = create_setup_intent(customer_id=stripe_cid)
    except stripe.error.StripeError as exc:
        logger.error("Stripe error creating SetupIntent for customer %s: %s", stripe_cid, exc)
        raise HTTPException(status_code=402, detail=str(exc.user_message or exc)) from exc
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="setup_intent",
                entity_type="setup_intent",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            logger.exception('setup_intent_audit_failed')
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
) -> dict[str, Any]:
    """Charge a previously saved payment method off-session."""
    stripe_cid = _require_stripe_customer(user)
    # Prevent double-charge on retry/double-click: forward an idempotency key to
    # Stripe (Idempotency-Key header wins; ChargeRequest.idempotency_key fallback).
    _idem = idempotency_key or body.idempotency_key
    try:
        intent = charge_saved_method(
            customer_id=stripe_cid,
            payment_method_id=method_id,
            amount_cents=body.amount_cents,
            currency=body.currency,
            metadata=body.metadata,
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
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
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
    user: CustomerUser = Depends(_current_portal_user),
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
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="ach_setup",
                entity_type="ach_setup",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            logger.exception('ach_setup_audit_failed')
    return {
        "source_id": source.id,
        "last4": getattr(source, "last4", None),
        "status": getattr(source, "status", None),
        "bank_name": getattr(source, "bank_name", body.bank_name),
    }


@router.delete("/methods/{method_id}", response_model=None)
def remove_payment_method(
    method_id: str,
    user: CustomerUser = Depends(_current_portal_user),
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
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="remove_payment_method",
                entity_type="payment_method",
                entity_id=str(method_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            logger.exception('remove_payment_method_audit_failed')
    return {"status": "removed", "payment_method_id": method_id}


# ---------------------------------------------------------------------------
# Stripe Connect — Connected Account Management
# ---------------------------------------------------------------------------

@router.post("/connect/setup-account", response_model=None)
def setup_connect_account(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a Stripe Express Connected Account for this tenant."""
    tid = str((getattr(request.state, "tenant", {}) or {}).get("id", ""))
    try:
        account = stripe.Account.create(
            type="express",
            metadata={"tenant_id": tid},
            capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
        )
        db.execute(
            text("UPDATE companies SET stripe_connect_account_id = :aid WHERE id = :tid"),
            {"aid": account.id, "tid": tid},
        )
        db.commit()
        log_audit_event_sync(db, tenant_id=tid, user_id=str(user.get("sub", "system")),
                             action="create", entity_type="stripe_connect_account",
                             entity_id=account.id, details={}, request=request)
        return {"stripe_account_id": account.id, "status": "created"}
    except Exception as e:
        logger.exception("stripe_connect_setup_failed")
        raise HTTPException(500, f"Stripe setup failed: {str(e)[:200]}") from None


@router.get("/connect/onboarding-link", response_model=None)
def get_connect_onboarding(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Get Stripe hosted onboarding URL."""
    tid = str((getattr(request.state, "tenant", {}) or {}).get("id", ""))
    row = db.execute(text("SELECT stripe_connect_account_id FROM companies WHERE id = :tid"), {"tid": tid}).mappings().first()
    account_id = row.get("stripe_connect_account_id") if row else None
    if not account_id:
        raise HTTPException(400, "No Stripe account. Call /connect/setup-account first.")
    try:
        base = os.getenv("GDX_BASE_URL", "https://gdx.example.com")
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=f"{base}/settings",
            return_url=f"{base}/settings?stripe=success",
            type="account_onboarding",
        )
        return {"url": link.url}
    except Exception as e:
        raise HTTPException(500, f"Onboarding link failed: {str(e)[:200]}") from None


@router.get("/connect/balance", response_model=None)
def get_connect_balance(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get tenant's Stripe Connect balance."""
    tid = str((getattr(request.state, "tenant", {}) or {}).get("id", ""))
    row = db.execute(text("SELECT stripe_connect_account_id FROM companies WHERE id = :tid"), {"tid": tid}).mappings().first()
    account_id = row.get("stripe_connect_account_id") if row else None
    if not account_id:
        return {"available": 0, "pending": 0, "currency": "usd", "connected": False}
    try:
        balance = stripe.Balance.retrieve(stripe_account=account_id)
        available = sum(b.amount for b in balance.available) if balance.available else 0
        pending = sum(b.amount for b in balance.pending) if balance.pending else 0
        return {"available": available, "pending": pending, "currency": "usd", "connected": True}
    except Exception as e:
        logging.getLogger(__name__).exception("get_connect_balance caught exception")
        # Generic error; full exception is logged above. (CodeQL stack-trace-exposure)
        return {"available": 0, "pending": 0, "error": "Unable to retrieve balance", "connected": True}


@router.post("/connect/webhook", response_model=None)
async def stripe_connect_webhook(request: Request):
    """Handle Stripe Connect webhook events."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        return {"status": "webhook_secret_not_configured"}
    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except Exception:
        raise HTTPException(400, "Invalid webhook signature") from None
    logger.info("Stripe webhook: %s", event["type"])
    if event["type"] == "payment_intent.succeeded":
        pi = event["data"]["object"]
        logger.info("Payment succeeded: %s amount=%s", pi["id"], pi["amount"])
    elif event["type"] == "account.updated":
        acct = event["data"]["object"]
        logger.info("Account updated: %s charges_enabled=%s", acct["id"], acct.get("charges_enabled"))
    return {"status": "received"}
