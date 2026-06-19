from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import jwt
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Document, Invoice, Job
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.modules.equipment.models import CustomerEquipment

log = logging.getLogger(__name__)

ACCESS_TTL_SECONDS = 60 * 60
MAGIC_LINK_TTL_MINUTES = 15
ALG = "HS256"
SIGN_KEY = os.getenv("JWT_SECRET", "dev-secret")
VERIFY_KEY = SIGN_KEY

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/portal/login")
router = APIRouter(
    prefix="/portal",
    tags=["customer_portal"],
    dependencies=[Depends(require_module("customer_portal"))],
)


class PortalLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class BookingIn(BaseModel):
    requested_date: datetime
    service_type: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=5000)


class MessageIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=10000)


class PortalPrincipal(BaseModel):
    user_id: UUID
    customer_id: UUID
    role: str = Field(max_length=50)


def send_portal_magic_link_email(to_email: str, magic_link: str) -> None:
    # Stubbed for tests/local flow; production integrations can override this.
    _ = (to_email, magic_link)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def issue_customer_access_token(customer_user: CustomerUser) -> str:
    claims = {
        "sub": str(customer_user.id),
        "role": "customer",
        "customer_id": str(customer_user.customer_id),
        "typ": "access",
        "exp": int((_now_utc() + timedelta(seconds=ACCESS_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(claims, SIGN_KEY, algorithm=ALG)


def _payment_status(invoice: Invoice) -> str:
    if invoice.status == "paid":
        return "paid"
    if float(invoice.balance_due or 0) > 0:
        return "unpaid"
    return "paid"


def get_current_portal_customer(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> PortalPrincipal:
    try:
        claims = jwt.decode(token, VERIFY_KEY, algorithms=[ALG])
        if claims.get("role") != "customer":
            raise HTTPException(status_code=401, detail="Invalid customer token")
        user_id = UUID(str(claims["sub"]))
        customer_id = UUID(str(claims["customer_id"]))
    except HTTPException:
        raise
    except (JWTError, KeyError, TypeError, ValueError):
        log.exception("portal_token_decode_failed")
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    user = db.execute(
        select(CustomerUser).where(
            CustomerUser.id == user_id,
            CustomerUser.customer_id == customer_id,
            CustomerUser.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Customer user not found")
    return PortalPrincipal(user_id=user.id, customer_id=user.customer_id, role="customer")


@router.post("/login", response_model=None)
def portal_login(payload: PortalLoginIn, request: Request, db: Session = Depends(get_db)) -> dict[str, bool]:
    user = db.execute(
        select(CustomerUser).where(
            func.lower(CustomerUser.email) == payload.email.strip().lower(),
            CustomerUser.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not user:
        return {"ok": True}

    token = secrets.token_urlsafe(32)
    user.portal_token = token
    user.portal_token_expires_at = _now_utc() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.id),
        action="portal_magic_link_sent",
        entity_type="customer_user",
        entity_id=str(user.id),
        details={"email": payload.email.strip().lower()},
    )
    db.commit()

    base = os.getenv("CUSTOMER_PORTAL_BASE_URL", "").rstrip("/")
    link = f"{base}/portal/verify?token={token}" if base else f"/portal/verify?token={token}"
    send_portal_magic_link_email(user.email, link)
    log.info("portal_magic_link_sent", extra={"customer_user_id": str(user.id)})
    return {"ok": True}


@router.get("/verify", response_model=None)
def portal_verify(token: str, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.execute(
        select(CustomerUser).where(
            CustomerUser.portal_token == token,
            CustomerUser.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired magic link")

    expires_at = _normalize_dt(user.portal_token_expires_at)
    if not expires_at or expires_at <= _now_utc():
        raise HTTPException(status_code=401, detail="Invalid or expired magic link")

    user.portal_token = None
    user.portal_token_expires_at = None
    user.last_login_at = _now_utc()
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.id),
        action="portal_login_verified",
        entity_type="customer_user",
        entity_id=str(user.id),
        details={"customer_id": str(user.customer_id)},
    )
    db.commit()

    return {"access_token": issue_customer_access_token(user), "token_type": "bearer"}


@router.get("/dashboard", response_model=None)
def portal_dashboard(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job_count = db.execute(
        select(func.count(Job.id)).where(Job.customer_id == principal.customer_id, Job.deleted_at.is_(None))
    ).scalar_one()
    invoice_count = db.execute(
        select(func.count(Invoice.id))
        .join(Job, Invoice.job_id == Job.id)
        .where(Job.customer_id == principal.customer_id, Invoice.deleted_at.is_(None))
    ).scalar_one()
    equipment_count = db.execute(
        select(func.count(CustomerEquipment.id)).where(
            CustomerEquipment.customer_id == principal.customer_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one()
    return {
        "customer_id": str(principal.customer_id),
        "counts": {"jobs": int(job_count), "invoices": int(invoice_count), "equipment": int(equipment_count)},
    }


@router.get("/jobs", response_model=None)
def portal_jobs(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Job)
        .where(Job.customer_id == principal.customer_id, Job.deleted_at.is_(None))
        .order_by(Job.created_at.desc())
    ).scalars()
    return [
        {
            "id": str(row.id),
            "title": row.title,
            "lifecycle_stage": row.lifecycle_stage,
            "dispatch_status": row.dispatch_status,
            "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.get("/invoices", response_model=None)
def portal_invoices(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Invoice)
        .join(Job, Invoice.job_id == Job.id)
        .where(Job.customer_id == principal.customer_id, Invoice.deleted_at.is_(None))
        .order_by(Invoice.created_at.desc())
    ).scalars()
    return [
        {
            "id": str(row.id),
            "invoice_number": row.invoice_number,
            "status": row.status,
            "payment_status": _payment_status(row),
            "total": float(row.total or 0),
            "balance_due": float(row.balance_due or 0),
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.post("/invoices/{invoice_id}/pay", response_model=None)
def portal_invoice_pay(
    invoice_id: UUID,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    invoice = db.execute(
        select(Invoice)
        .join(Job, Invoice.job_id == Job.id)
        .where(
            Invoice.id == invoice_id,
            Job.customer_id == principal.customer_id,
            Invoice.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    amount_due = Decimal(str(invoice.balance_due if invoice.balance_due is not None else invoice.total))
    if amount_due <= 0:
        amount_due = Decimal(str(invoice.total or 0))
    amount_cents = int(amount_due * 100)

    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        metadata={"invoice_id": str(invoice.id), "customer_id": str(principal.customer_id)},
    )
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
                action="portal_invoice_pay",
                entity_type="portal_invoice_pay",
                entity_id=str(invoice_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('portal_invoice_pay_audit_failed')
    return {
        "payment_intent_id": intent.id,
        "client_secret": intent.client_secret,
        "status": getattr(intent, "status", None),
    }


@router.get("/equipment", response_model=None)
def portal_equipment(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.customer_id == principal.customer_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "customer_id": str(row.customer_id),
            "equipment_type": row.equipment_type,
            "manufacturer": row.manufacturer,
            "model": row.model,
            "serial_number": row.serial_number,
            "installation_date": row.installation_date.isoformat() if row.installation_date else None,
            "last_service_date": row.last_service_date.isoformat() if row.last_service_date else None,
        }
        for row in rows
    ]


@router.get("/documents", response_model=None)
def portal_documents(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Document)
        .where(Document.customer_id == principal.customer_id, Document.deleted_at.is_(None))
        .order_by(Document.uploaded_at.desc())
    ).scalars()
    return [
        {
            "id": str(row.id),
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "filename": row.filename,
            "original_name": row.original_name,
            "title": row.title,
            "description": row.description,
            "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
        }
        for row in rows
    ]


@router.post("/booking", response_model=None)
def portal_booking(
    payload: BookingIn,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import PortalBookingRequest
    booking_id = str(uuid4())
    created_at = _now_utc().isoformat()
    db.add(PortalBookingRequest(
        id=booking_id,
        customer_id=str(principal.customer_id),
        requested_date=payload.requested_date.astimezone(UTC).isoformat(),
        service_type=payload.service_type.strip(),
        notes=payload.notes.strip() if payload.notes else None,
        status="requested",
        created_at=created_at,
    ))
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(principal.user_id),
        action="portal_booking_created",
        entity_type="booking_request",
        entity_id=booking_id,
        details={"customer_id": str(principal.customer_id), "service_type": payload.service_type.strip()},
    )
    db.commit()
    return {"id": booking_id, "status": "requested"}


@router.post("/message", response_model=None)
def portal_message(
    payload: MessageIn,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    from gdx_dispatch.models.tenant_models import PortalMessage
    message_id = str(uuid4())
    created_at = _now_utc().isoformat()
    db.add(PortalMessage(
        id=message_id,
        customer_id=str(principal.customer_id),
        subject=payload.subject.strip(),
        message=payload.message.strip(),
        status="sent",
        created_at=created_at,
    ))
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(principal.user_id),
        action="portal_message_sent",
        entity_type="portal_message",
        entity_id=message_id,
        details={"customer_id": str(principal.customer_id), "subject": payload.subject.strip()},
    )
    db.commit()
    return {"id": message_id, "status": "sent"}
