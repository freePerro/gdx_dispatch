from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import log_audit_event, log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import SessionLocal, get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.stripe_connect import (
    create_account_link,
    create_connected_account,
    create_payment_intent,
    get_account_status,
)
from gdx_dispatch.models.tenant_models import AppSettings

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/stripe/connect",
    tags=["stripe-connect"],
    dependencies=[Depends(require_module("stripe_connect"))],
)


class OnboardRequest(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    return_url: str = Field(min_length=1, max_length=2048)
    refresh_url: str = Field(min_length=1, max_length=2048)
    account_type: str = Field(default="express", max_length=30)


class PaymentIntentRequest(BaseModel):
    account_id: str | None = Field(default=None, max_length=120)
    amount_cents: int = Field(gt=0, le=100_000_000)
    currency: str = Field(default="usd", min_length=3, max_length=3, pattern=r"^[a-z]{3}$")
    fee_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    metadata: dict[str, Any] | None = None


def _get_stripe_key() -> str:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="Stripe payments not configured")
    return key


def _get_tenant_account_id(request: Request, control_db: Session, fallback_account_id: str | None = None) -> str:
    if fallback_account_id:
        return fallback_account_id

    tenant_state = getattr(request.state, "tenant", {}) or {}
    tenant_id = tenant_state.get("id")
    if not tenant_id:
        raise HTTPException(status_code=404, detail="Tenant context not found")

    account_id = None
    if hasattr(control_db, "tenant"):
        tenant = control_db.tenant
        account_id = getattr(tenant, "stripe_connect_account_id", None) if tenant else None
    else:
        tenant_lookup: UUID | str
        try:
            tenant_lookup = UUID(str(tenant_id))
        except (TypeError, ValueError):
            log.exception("stripe_connect_tenant_uuid_parse_failed")
            tenant_lookup = str(tenant_id)
        row = control_db.execute(
            text(
                "SELECT stripe_connect_account_id FROM tenants WHERE id = :tenant_id LIMIT 1"
            ),
            {"tenant_id": str(tenant_lookup)},
        ).mappings().first()
        account_id = row.get("stripe_connect_account_id") if row else None

    if not account_id:
        raise HTTPException(status_code=404, detail="Stripe Connect account not found for tenant")
    return str(account_id)


def _actor_id(user: dict[str, Any]) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _load_tenant_integrations(tenant_db: Session) -> dict[str, Any]:
    from sqlalchemy import select as _select
    # Use scalar_one_or_none() to stay compatible with the existing FakeTenantDB mock
    # which returns an object with that method. The query fetches the integrations JSON column.
    raw = tenant_db.execute(
        _select(AppSettings.integrations).limit(1)
    ).scalar_one_or_none()
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            log.exception("stripe_connect_integrations_json_decode_failed")
            return {}
    return {}


def _resolve_fee_percent(body_fee_percent: float | None, tenant_db: Session) -> float:
    if body_fee_percent is not None:
        return body_fee_percent

    integrations = _load_tenant_integrations(tenant_db)
    stripe_cfg = integrations.get("stripe")
    configured = stripe_cfg.get("platform_fee_percent") if isinstance(stripe_cfg, dict) else None
    if configured is None:
        return 2.0

    fee_percent = float(configured)
    if fee_percent < 0:
        raise ValueError("platform fee percent must be non-negative")
    return fee_percent


def _update_tenant_connect_account(account_id: str, tenant_id: str | None = None) -> int:
    db = SessionLocal()
    try:
        if tenant_id:
            result = db.execute(
                text(
                    """
                    UPDATE tenants
                    SET stripe_connect_account_id = :account_id
                    WHERE id = :tenant_id
                    """
                ),
                {"account_id": account_id, "tenant_id": tenant_id},
            )
        else:
            result = db.execute(
                text(
                    """
                    UPDATE tenants
                    SET stripe_connect_account_id = :account_id
                    WHERE stripe_connect_account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            )
        db.commit()
        return int(result.rowcount or 0)
    except Exception:
        log.exception("stripe_connect_update_tenant_failed", extra={"tenant_id": tenant_id, "account_id": account_id})
        db.rollback()
        return 0
    finally:
        db.close()


@router.post("/onboard")
def onboard_tenant(
    body: OnboardRequest,
    request: Request,
    _: dict[str, str] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> dict[str, str]:
    stripe_secret_key = _get_stripe_key()
    tenant_state = getattr(request.state, "tenant", {}) or {}
    tenant_id = tenant_state.get("id")
    actor = _actor_id(_)

    try:
        account = create_connected_account(
            tenant_name=body.tenant_name,
            email=body.email,
            account_type=body.account_type,
            stripe_secret_key=stripe_secret_key,
            metadata={"tenant_id": str(tenant_id)} if tenant_id else None,
        )
        link = create_account_link(
            account_id=account.id,
            return_url=body.return_url,
            refresh_url=body.refresh_url,
            stripe_secret_key=stripe_secret_key,
        )

        if tenant_id:
            if hasattr(control_db, "tenant"):
                tenant = control_db.tenant
                if tenant is not None:
                    tenant.stripe_connect_account_id = account.id
                    control_db.commit()
            else:
                control_db.execute(
                    text(
                        "UPDATE tenants SET stripe_connect_account_id = :account_id WHERE id = :tenant_id"
                    ),
                    {"account_id": account.id, "tenant_id": str(tenant_id)},
                )
                control_db.commit()

        try:
            log_audit_event_sync(
                db=tenant_db,
                tenant_id=str(tenant_id) if tenant_id else None,
                user_id=actor,
                action="stripe_connect_onboarded",
                entity_type="stripe_connect_account",
                entity_id=str(account.id),
                details={"email": body.email, "account_type": body.account_type},
                ip_address=request.client.host if request.client else None,
                request=request,
            )
            tenant_db.commit()
        except Exception:
            log.exception("stripe_connect_onboard_audit_failed")

        log.info("stripe_connect_onboarded", extra={"tenant_id": str(tenant_id or ""), "account_id": account.id})
        return {"account_id": account.id, "onboarding_url": link.url}
    except IntegrityError as exc:
        log.exception("stripe_connect_onboard_integrity_error")
        raise HTTPException(status_code=409, detail="stripe connect account conflict") from exc
    except ValueError as exc:
        log.exception("stripe_connect_onboard_value_error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("stripe_connect_onboard_failed")
        raise HTTPException(status_code=500, detail="failed to onboard tenant") from exc


@router.get("/status")
def stripe_connect_status(
    request: Request,
    account_id: str | None = None,
    _: dict[str, str] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> dict[str, Any]:
    stripe_secret_key = _get_stripe_key()
    tenant = getattr(request.state, "tenant", {}) or {}
    actor = _actor_id(_)
    try:
        resolved_account_id = _get_tenant_account_id(request, control_db, fallback_account_id=account_id)
        status = get_account_status(resolved_account_id, stripe_secret_key=stripe_secret_key)
        try:
            log_audit_event_sync(
                db=tenant_db,
                tenant_id=str(tenant.get("id", "")) or None,
                user_id=actor,
                action="stripe_connect_status_checked",
                entity_type="stripe_connect_account",
                entity_id=resolved_account_id,
                details={
                    "charges_enabled": bool(status.get("charges_enabled")),
                    "payouts_enabled": bool(status.get("payouts_enabled")),
                },
                ip_address=request.client.host if request.client else None,
                request=request,
            )
            tenant_db.commit()
        except Exception:
            log.exception("stripe_connect_status_audit_failed")
        return status
    except IntegrityError as exc:
        log.exception("stripe_connect_status_integrity_error")
        raise HTTPException(status_code=409, detail="stripe connect status conflict") from exc
    except ValueError as exc:
        log.exception("stripe_connect_status_value_error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("stripe_connect_status_failed")
        raise HTTPException(status_code=500, detail="failed to load stripe connect status") from exc


@router.post("/payment-intent")
def create_connect_payment_intent(
    body: PaymentIntentRequest,
    request: Request,
    _: dict[str, str] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> dict[str, Any]:
    stripe_secret_key = _get_stripe_key()
    tenant = getattr(request.state, "tenant", {}) or {}
    try:
        resolved_account_id = _get_tenant_account_id(request, control_db, fallback_account_id=body.account_id)
        fee_percent = _resolve_fee_percent(body.fee_percent, tenant_db)
        metadata = dict(body.metadata or {})
        if fee_percent > 0:
            metadata["platform_fee_cents"] = int(round(body.amount_cents * (fee_percent / 100.0)))

        application_fee_amount = metadata.get("platform_fee_cents", 0)
        intent = create_payment_intent(
            account_id=resolved_account_id,
            amount_cents=body.amount_cents,
            currency=body.currency,
            metadata=metadata,
            stripe_secret_key=stripe_secret_key,
        )
        try:
            log_audit_event_sync(
                db=tenant_db,
                tenant_id=str(tenant.get("id", "")) or None,
                user_id=_actor_id(_),
                action="payment_intent_created",
                entity_type="stripe_payment_intent",
                entity_id=str(getattr(intent, "id", "")),
                details={
                    "account_id": resolved_account_id,
                    "amount_cents": body.amount_cents,
                    "currency": body.currency,
                    "platform_fee_percent": fee_percent,
                    "platform_fee_cents": application_fee_amount,
                },
                ip_address=request.client.host if request.client else None,
                request=request,
            )
            tenant_db.commit()
        except Exception:
            log.exception("stripe_connect_payment_intent_audit_failed")

        return {
            "payment_intent_id": intent.id,
            "client_secret": getattr(intent, "client_secret", None),
            "application_fee_amount": getattr(intent, "application_fee_amount", application_fee_amount),
        }
    except IntegrityError as exc:
        log.exception("stripe_connect_payment_intent_integrity_error")
        raise HTTPException(status_code=409, detail="payment intent conflict") from exc
    except ValueError as exc:
        log.exception("stripe_connect_payment_intent_value_error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("stripe_connect_payment_intent_failed")
        raise HTTPException(status_code=500, detail="failed to create payment intent") from exc


@router.get("/balance")
def get_connect_balance(
    request: Request,
    account_id: str | None = None,
    _: dict[str, str] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_db = tenant_db
    stripe_secret_key = _get_stripe_key()
    resolved_account_id = _get_tenant_account_id(request, control_db, fallback_account_id=account_id)

    stripe.api_key = stripe_secret_key
    balance = stripe.Balance.retrieve(stripe_account=resolved_account_id)
    return dict(balance)


@router.post("/webhook")
async def stripe_connect_webhook(request: Request) -> dict[str, Any]:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET", os.getenv("STRIPE_WEBHOOK_SECRET", ""))

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
        )
    except Exception as exc:
        log.exception("stripe_connect_webhook_invalid_signature")
        raise HTTPException(status_code=400, detail=f"invalid signature: {exc}") from exc

    event_type = event.get("type", "")
    event_object = event.get("data", {}).get("object", {})
    try:
        if event_type == "account.updated":
            account_id = str(event_object.get("id") or "")
            metadata = event_object.get("metadata") if isinstance(event_object, dict) else {}
            tenant_id = metadata.get("tenant_id") if isinstance(metadata, dict) else None
            if account_id:
                if tenant_id:
                    _update_tenant_connect_account(account_id, tenant_id=str(tenant_id))
                else:
                    _update_tenant_connect_account(account_id)

            result = {
                "status": "processed",
                "event_type": event_type,
                "account_id": account_id,
                "charges_enabled": event_object.get("charges_enabled"),
                "payouts_enabled": event_object.get("payouts_enabled"),
            }
            await _log_webhook_processed(request, event_type, account_id, result)
            return result

        if event_type == "payment_intent.succeeded":
            result = {
                "status": "processed",
                "event_type": event_type,
                "payment_intent_id": event_object.get("id"),
                "amount": event_object.get("amount"),
            }
            await _log_webhook_processed(request, event_type, str(event_object.get("id") or ""), result)
            return result

        result = {"status": "ignored", "event_type": event_type}
        await _log_webhook_processed(request, event_type, str(event.get("id") or ""), result)
        return result
    except IntegrityError as exc:
        log.exception("stripe_connect_webhook_integrity_error")
        raise HTTPException(status_code=409, detail="webhook conflict") from exc
    except ValueError as exc:
        log.exception("stripe_connect_webhook_value_error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("stripe_connect_webhook_processing_failed")
        raise HTTPException(status_code=500, detail="failed to process webhook") from exc


async def _log_webhook_processed(
    request: Request,
    event_type: str,
    entity_id: str,
    details: dict[str, Any],
) -> None:
    tenant = getattr(request.state, "tenant", {}) or {}
    db_url = str(tenant.get("db_url") or "")
    if not db_url:
        return

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {},
        poolclass=StaticPool if db_url.startswith("sqlite") else None,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        await log_audit_event(
            db=db,
            tenant_id=str(tenant.get("id", "")) or None,
            user_id="system",
            action="webhook_processed",
            entity_type="stripe_webhook",
            entity_id=entity_id,
            details={"event_type": event_type, **details},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
    except SQLAlchemyError:
        log.exception("stripe_connect_webhook_audit_log_failed", extra={"event_type": event_type})
        db.rollback()
    finally:
        db.close()
        engine.dispose()
