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
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Document, Invoice, Job
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.modules.equipment.models import CustomerEquipment
from gdx_dispatch.modules.estimates_features import effective_hide_line_prices, get_features
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

log = logging.getLogger(__name__)

ACCESS_TTL_SECONDS = 60 * 60
REMEMBER_ME_TTL_SECONDS = 60 * 60 * 24 * 30  # "Remember me" — 30-day session
MAGIC_LINK_TTL_MINUTES = 15
INVITE_LINK_TTL_DAYS = 7
# Statuses a customer may see. Drafts stay internal until staff hits Send.
CUSTOMER_VISIBLE_ESTIMATE_STATUSES = ("sent", "accepted", "declined", "expired")
# Browser-renderable image types only. Staff uploads also allow HEIC/HEIF
# (tech iPhone photos) but Chrome/Firefox can't decode those — advertising
# them to the portal would show broken thumbnails.
PORTAL_RENDERABLE_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
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


class PortalPasswordLoginIn(BaseModel):
    # Bounds mirror the staff LoginBody: 254 = RFC 5321 max, 128 = bcrypt-safe ceiling.
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = False


class PortalSetPasswordIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


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


def _company_name(db: Session) -> str:
    settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
    if settings_obj and settings_obj.company_name:
        return settings_obj.company_name
    return "Your Service Company"


def _portal_link_base(request: Request) -> str:
    # CUSTOMER_PORTAL_BASE_URL wins so the portal can live on its own domain;
    # otherwise the link is absolute against whatever host served this request
    # (an emailed relative link would be dead on arrival).
    base = os.getenv("CUSTOMER_PORTAL_BASE_URL", "").rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def _magic_link_html(company_name: str, magic_link: str, expires_text: str) -> str:
    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 560px; margin: 0 auto;">
      <h2 style="color: #1e293b;">{company_name}</h2>
      <p>Use the button below to sign in to your customer portal, where you can
      view your estimates, invoices, and service history.</p>
      <p style="margin: 24px 0;">
        <a href="{magic_link}" style="background: #2563eb; color: #ffffff; padding: 12px 24px;
           border-radius: 6px; text-decoration: none; display: inline-block;">Open My Portal</a>
      </p>
      <p style="color: #6b7280; font-size: 13px;">This link {expires_text} and can only be used once.
      If you didn't request it, you can safely ignore this email.</p>
      <p style="color: #6b7280; font-size: 13px;">Button not working? Copy this address into your
      browser:<br>{magic_link}</p>
    </div>
    """


def send_portal_magic_link_email(
    db: Session,
    tenant_id: str,
    to_email: str,
    magic_link: str,
    *,
    company_name: str = "",
    to_name: str = "",
    sender_user_id: str | None = None,
    expires_text: str = f"expires in {MAGIC_LINK_TTL_MINUTES} minutes",
) -> tuple[bool, str | None]:
    """Deliver a portal sign-in link. Returns (sent, skip_reason).

    Tests monkeypatch this; production delivery rides the unified
    transactional-email helper (Outlook Graph when sender_user_id is a
    connected staff user, SMTP via email_settings otherwise).
    """
    from gdx_dispatch.core.transactional_email import send_transactional_email

    company = company_name or _company_name(db)
    sent, _provider, skip_reason = send_transactional_email(
        tenant_db=db,
        tenant_id=tenant_id,
        user_id=sender_user_id,
        to_email=to_email,
        to_name=to_name,
        subject=f"Your {company} customer portal link",
        html_body=_magic_link_html(company, magic_link, expires_text),
    )
    return sent, skip_reason


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def issue_customer_access_token(customer_user: CustomerUser, *, remember: bool = False) -> str:
    ttl = REMEMBER_ME_TTL_SECONDS if remember else ACCESS_TTL_SECONDS
    claims = {
        "sub": str(customer_user.id),
        "role": "customer",
        "customer_id": str(customer_user.customer_id),
        "typ": "access",
        "exp": int((_now_utc() + timedelta(seconds=ttl)).timestamp()),
    }
    return jwt.encode(claims, SIGN_KEY, algorithm=ALG)


def _hash_portal_password(password: str) -> str:
    """bcrypt — the same scheme the rest of the app writes (admin_ops, bootstrap)."""
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_portal_password(password: str, pw_hash: str | None) -> bool:
    """Prefix-dispatch verify, mirroring the staff verifier in routers/auth/core.py:
    ``$2`` → bcrypt, ``pbkdf2:``/``scrypt:`` → werkzeug."""
    if not pw_hash:
        return False
    if pw_hash.startswith("$2"):
        import bcrypt

        try:
            return bcrypt.checkpw(password.encode(), pw_hash.encode())
        except ValueError:
            return False
    if pw_hash.startswith(("pbkdf2:", "scrypt:")):
        from werkzeug.security import check_password_hash

        return check_password_hash(pw_hash, password)
    return False


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
    # .first() with a deterministic order, not scalar_one_or_none():
    # customer emails carry no unique constraint, and a duplicate must not
    # turn the public login endpoint into a 500.
    user = db.execute(
        select(CustomerUser)
        .where(
            func.lower(CustomerUser.email) == payload.email.strip().lower(),
            CustomerUser.is_active.is_(True),
        )
        .order_by(CustomerUser.created_at.desc())
    ).scalars().first()
    if not user:
        return {"ok": True}

    # Reuse a still-valid longer-lived token (a pending staff invite) instead
    # of clobbering it — this endpoint is public, and anyone who knows the
    # email could otherwise invalidate an invite the customer hasn't opened.
    existing_expiry = _normalize_dt(user.portal_token_expires_at)
    login_expiry = _now_utc() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
    if user.portal_token and existing_expiry and existing_expiry > login_expiry:
        token = user.portal_token
    else:
        token = secrets.token_urlsafe(32)
        user.portal_token = token
        user.portal_token_expires_at = login_expiry
    db.commit()

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    link = f"{_portal_link_base(request)}/customer-portal?token={token}"
    sent, skip_reason = send_portal_magic_link_email(db, tenant_id, user.email, link)

    # The HTTP response stays {"ok": true} either way (anti-enumeration),
    # but the audit trail must record what actually happened.
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(user.id),
        action="portal_magic_link_sent" if sent else "portal_magic_link_send_failed",
        entity_type="customer_user",
        entity_id=str(user.id),
        details={"email": payload.email.strip().lower(), "email_sent": bool(sent), "skip_reason": skip_reason},
    )
    db.commit()
    if sent:
        log.info("portal_magic_link_sent", extra={"customer_user_id": str(user.id)})
    else:
        log.warning(
            "portal_magic_link_send_failed customer_user=%s reason=%s", user.id, skip_reason
        )
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


@router.post("/login/password", response_model=None)
def portal_password_login(
    payload: PortalPasswordLoginIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, str]:
    # Password sign-in (opt-in). Magic-link stays the onboarding + forgot-password
    # path. Brute-force throttling is the strict per-IP `auth` limit — this path is
    # covered by TenantRateLimitMiddleware._AUTH_PREFIXES ("/portal/login").
    email = payload.email.strip().lower()
    user = db.execute(
        select(CustomerUser)
        .where(func.lower(CustomerUser.email) == email, CustomerUser.is_active.is_(True))
        .order_by(CustomerUser.created_at.desc())
    ).scalars().first()
    ok = _verify_portal_password(payload.password, user.password_hash) if user else False
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    if not ok:
        # Audit BEFORE raising so the trail captures the attempt + source IP. One
        # generic 401 for no-user / no-password / bad-password — anti-enumeration.
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str(user.id) if user else "anonymous",
            action="portal_password_login_failed",
            entity_type="customer_user",
            entity_id=email,
            details={"email": email},
            request=request,
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user.last_login_at = _now_utc()
    db.commit()
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(user.id),
        action="portal_password_login",
        entity_type="customer_user",
        entity_id=str(user.id),
        details={"remember": bool(payload.remember)},
        request=request,
    )
    db.commit()
    return {
        "access_token": issue_customer_access_token(user, remember=payload.remember),
        "token_type": "bearer",
    }


@router.post("/password", response_model=None)
def portal_set_password(
    payload: PortalSetPasswordIn,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    # Any authenticated portal session may set/rotate the password. This is what
    # lets magic-link double as "forgot password": sign in via link, set a new one.
    # No current-password challenge — a valid session already has full account
    # access, so requiring it would only break recovery.
    user = db.execute(
        select(CustomerUser).where(
            CustomerUser.id == principal.user_id, CustomerUser.is_active.is_(True)
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Customer user not found")
    user.password_hash = _hash_portal_password(payload.new_password)
    db.commit()
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(user.id),
        action="portal_password_set",
        entity_type="customer_user",
        entity_id=str(user.id),
        details={},
        request=request,
    )
    db.commit()
    return {"ok": True}


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


@router.get("/context", response_model=None)
def portal_context(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
    customer = db.execute(
        select(Customer).where(Customer.id == principal.customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    return {
        "company": {
            "name": (settings_obj.company_name if settings_obj else "") or "Your Service Company",
            "phone": settings_obj.phone if settings_obj else "",
            "email": settings_obj.email if settings_obj else "",
            "address": settings_obj.address if settings_obj else "",
            "logo": settings_obj.logo if settings_obj else "",
        },
        "customer": {
            "id": str(principal.customer_id),
            "name": customer.name if customer else "",
        },
    }


def _portal_estimate_totals(estimate: Estimate, db: Session) -> dict[str, Any]:
    """Tax-inclusive totals with a LOUD degraded flag. When the totals engine
    fails, the customer sees the pre-tax subtotal — tax_unavailable=True lets
    the UI say so instead of presenting it as the final number."""
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    try:
        totals = compute_estimate_totals(estimate, db)
        return {
            "subtotal": totals["subtotal"],
            "discount": totals["discount"],
            "tax": totals["tax"],
            "tax_rate_pct": totals["tax_rate_pct"],
            "total": totals["total"],
            "tax_unavailable": False,
        }
    except Exception:
        log.exception("portal_estimate_totals_failed estimate=%s", estimate.id)
        subtotal = float(estimate.total or 0)
        return {
            "subtotal": subtotal,
            "discount": 0.0,
            "tax": 0.0,
            "tax_rate_pct": 0.0,
            "total": subtotal,
            "tax_unavailable": True,
        }


def _serialize_portal_estimate(
    estimate: Estimate, db: Session, totals: dict[str, Any] | None = None
) -> dict[str, Any]:
    if totals is None:
        totals = _portal_estimate_totals(estimate, db)
    grand_total = totals["total"]
    return {
        "id": str(estimate.id),
        "estimate_number": estimate.estimate_number,
        "label": estimate.label,
        "description": estimate.description,
        "status": estimate.status,
        "total": grand_total,
        "valid_until": estimate.valid_until.isoformat() if estimate.valid_until else None,
        "sent_at": estimate.sent_at.isoformat() if estimate.sent_at else None,
        "accepted_at": estimate.accepted_at.isoformat() if estimate.accepted_at else None,
        "declined_at": estimate.declined_at.isoformat() if estimate.declined_at else None,
        "created_at": estimate.created_at.isoformat() if estimate.created_at else None,
    }


@router.get("/estimates", response_model=None)
def portal_estimates(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Estimate)
        .where(
            Estimate.customer_id == principal.customer_id,
            Estimate.deleted_at.is_(None),
            Estimate.status.in_(CUSTOMER_VISIBLE_ESTIMATE_STATUSES),
        )
        .order_by(Estimate.created_at.desc())
    ).scalars()
    return [_serialize_portal_estimate(row, db) for row in rows]


@router.get("/estimates/{estimate_id}", response_model=None)
def portal_estimate_detail(
    estimate_id: UUID,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    estimate = _get_customer_estimate_or_404(estimate_id, principal, db)
    # Compute once and thread through — card total and breakdown total must
    # never disagree within one response.
    totals = _portal_estimate_totals(estimate, db)
    body = _serialize_portal_estimate(estimate, db, totals)
    body["jobsite_address"] = estimate.jobsite_address
    body["declined_reason"] = estimate.declined_reason
    body["totals"] = totals

    # Per-line prices are a customer-facing surface: honor the tri-state
    # hide_line_prices (per-estimate override wins, else the tenant default).
    # This is a JSON API, so the values are STRIPPED server-side, not just
    # hidden in the template — otherwise they'd leak in the network response.
    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or estimate.company_id or "")
    hide_prices = effective_hide_line_prices(
        getattr(estimate, "hide_line_prices", None), get_features(tenant_id).hide_line_prices
    )
    body["hide_line_prices"] = hide_prices

    lines = db.execute(
        select(EstimateLine)
        .where(EstimateLine.estimate_id == estimate.id)
        .order_by(EstimateLine.sort_order)
    ).scalars().all()
    body["lines"] = [
        {
            "id": str(line.id),
            "description": line.description,
            "quantity": float(line.quantity or 0),
            **(
                {}
                if hide_prices
                else {
                    "unit_price": float(line.unit_price or 0),
                    "line_total": float(line.line_total or 0),
                }
            ),
        }
        for line in lines
    ]

    # Image attachments only (door photos/renderings staff attach to the
    # estimate) — PDFs and other docs stay staff-side for now.
    images = db.execute(
        select(Document)
        .where(Document.estimate_id == estimate.id, Document.deleted_at.is_(None))
        .order_by(Document.uploaded_at.desc())
    ).scalars().all()
    body["images"] = [
        {
            "id": str(doc.id),
            "original_name": doc.original_name,
            "content_type": doc.content_type,
            "url": f"/portal/estimates/{estimate.id}/attachments/{doc.id}",
        }
        for doc in images
        if (doc.content_type or "").lower() in PORTAL_RENDERABLE_IMAGE_TYPES
    ]
    return body


@router.get("/estimates/{estimate_id}/attachments/{document_id}", response_model=None)
def portal_estimate_attachment(
    estimate_id: UUID,
    document_id: UUID,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve an image attached to the customer's own estimate. Mirrors the
    staff download route's realpath guard; images only — a customer link
    must never become a generic file-serving oracle."""
    estimate = _get_customer_estimate_or_404(estimate_id, principal, db)
    doc = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.estimate_id == estimate.id,
            Document.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not doc or (doc.content_type or "").lower() not in PORTAL_RENDERABLE_IMAGE_TYPES:
        raise HTTPException(status_code=404, detail="Attachment not found")

    from gdx_dispatch.routers.estimates import _attachment_dir

    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or estimate.company_id or "")
    base = str(_attachment_dir(tenant_id, str(estimate.id)))
    fullpath = os.path.realpath(os.path.join(base, doc.filename))
    if not fullpath.startswith(base + os.sep) or not os.path.isfile(fullpath):
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(
        path=fullpath,
        media_type=doc.content_type or "application/octet-stream",
        filename=doc.original_name,
    )


def _get_customer_estimate_or_404(estimate_id: UUID, principal: PortalPrincipal, db: Session) -> Estimate:
    estimate = db.execute(
        select(Estimate).where(
            Estimate.id == estimate_id,
            Estimate.customer_id == principal.customer_id,
            Estimate.deleted_at.is_(None),
            Estimate.status.in_(CUSTOMER_VISIBLE_ESTIMATE_STATUSES),
        )
    ).scalar_one_or_none()
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return estimate


class DeclineEstimateIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/estimates/{estimate_id}/accept", response_model=None)
def portal_estimate_accept(
    estimate_id: UUID,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    estimate = _get_customer_estimate_or_404(estimate_id, principal, db)
    if estimate.status == "accepted":
        raise HTTPException(status_code=409, detail="already accepted")
    if estimate.status != "sent":
        raise HTTPException(status_code=409, detail="estimate is not open for acceptance")

    estimate.status = "accepted"
    estimate.accepted_at = _now_utc()
    estimate.updated_at = _now_utc()
    db.commit()
    db.refresh(estimate)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    actor = f"portal:{principal.user_id}"
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=actor,
        action="portal_estimate_accepted",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"customer_id": str(principal.customer_id)},
    )
    db.commit()

    # Mirror the staff accept flow (2026-05-13 directive: accept = job
    # created) so a portal acceptance lands on the dispatch board too.
    if estimate.job_id is None:
        try:
            from gdx_dispatch.routers.estimates import _create_job_from_estimate

            _create_job_from_estimate(estimate, db, actor)
        except Exception:
            log.exception("portal_accept_auto_convert_failed estimate=%s", estimate.id)
            try:
                log_audit_event_sync(
                    db=db,
                    tenant_id=tenant_id,
                    user_id=actor,
                    action="estimate_auto_convert_failed",
                    entity_type="estimate",
                    entity_id=str(estimate.id),
                    details={"source": "portal"},
                )
                db.commit()
            except Exception:
                log.exception("portal_accept_audit_failed")

    return _serialize_portal_estimate(estimate, db)


@router.post("/estimates/{estimate_id}/decline", response_model=None)
def portal_estimate_decline(
    estimate_id: UUID,
    request: Request,
    payload: DeclineEstimateIn | None = None,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    estimate = _get_customer_estimate_or_404(estimate_id, principal, db)
    if estimate.status == "declined":
        raise HTTPException(status_code=409, detail="already declined")
    if estimate.status != "sent":
        raise HTTPException(status_code=409, detail="estimate is not open for decline")

    reason = payload.reason.strip() if payload and payload.reason else None
    estimate.status = "declined"
    estimate.declined_at = _now_utc()
    estimate.declined_reason = reason
    estimate.updated_at = _now_utc()
    db.commit()
    db.refresh(estimate)

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=f"portal:{principal.user_id}",
        action="portal_estimate_declined",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"customer_id": str(principal.customer_id), "reason": reason},
    )
    db.commit()
    return _serialize_portal_estimate(estimate, db)


# ---------------------------------------------------------------------------
# Staff-side portal management (/api/portal) — backs PortalView.vue. These
# replaced the ui_compat empty-list stubs; they require a staff login, not a
# customer JWT.
# ---------------------------------------------------------------------------

from gdx_dispatch.core.modules import require_permission  # noqa: E402
from gdx_dispatch.routers.auth import get_current_user  # noqa: E402

staff_router = APIRouter(
    prefix="/api/portal",
    tags=["customer_portal_admin"],
    dependencies=[Depends(require_module("customer_portal"))],
)


class PortalToggleIn(BaseModel):
    portal_enabled: bool
    email: str | None = Field(default=None, max_length=254)


class PortalInviteIn(BaseModel):
    customer_id: UUID
    email: str | None = Field(default=None, max_length=254)


def _resolve_portal_email(customer: Customer, override: str | None) -> str:
    email = (override or "").strip().lower() or (customer.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Customer has no email address on file")
    return email


def _get_or_create_customer_user(db: Session, customer: Customer, email: str) -> CustomerUser:
    # customer_users has no unique constraint on customer_id, so pick
    # deterministically: active record first, then newest.
    user = db.execute(
        select(CustomerUser)
        .where(CustomerUser.customer_id == customer.id)
        .order_by(CustomerUser.is_active.desc(), CustomerUser.created_at.desc())
    ).scalars().first()
    if user:
        user.email = email
        user.is_active = True
    else:
        user = CustomerUser(customer_id=customer.id, email=email, is_active=True)
        db.add(user)
    db.commit()
    db.refresh(user)
    return user


@staff_router.get("/info", response_model=None)
def portal_module_info(_: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"module": "customer_portal", "enabled": True}


@staff_router.get("", response_model=None, dependencies=[Depends(require_permission("customers.read_all"))])
def portal_admin_list(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    customers = db.execute(
        select(Customer).where(Customer.deleted_at.is_(None)).order_by(Customer.name)
    ).scalars().all()
    users_by_customer: dict[Any, CustomerUser] = {}
    for user in db.execute(select(CustomerUser)).scalars():
        # Prefer the active record when a customer somehow has several.
        existing = users_by_customer.get(user.customer_id)
        if existing is None or (user.is_active and not existing.is_active):
            users_by_customer[user.customer_id] = user

    payments_by_customer = {
        row[0]: int(row[1])
        for row in db.execute(
            select(Job.customer_id, func.count(Invoice.id))
            .join(Invoice, Invoice.job_id == Job.id)
            .where(Invoice.status == "paid", Invoice.deleted_at.is_(None), Job.deleted_at.is_(None))
            .group_by(Job.customer_id)
        )
    }

    entries = []
    for customer in customers:
        user = users_by_customer.get(customer.id)
        entries.append(
            {
                "id": str(customer.id),
                "customer_name": customer.name,
                "email": (user.email if user else None) or customer.email,
                "portal_enabled": bool(user and user.is_active),
                "last_login": user.last_login_at.isoformat() if user and user.last_login_at else None,
                "payments_made": payments_by_customer.get(customer.id, 0),
            }
        )
    return entries


@staff_router.patch("/{customer_id}", response_model=None, dependencies=[Depends(require_permission("customers.write"))])
def portal_admin_toggle(
    customer_id: UUID,
    payload: PortalToggleIn,
    request: Request,
    staff: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    customer = db.execute(
        select(Customer).where(Customer.id == customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    if payload.portal_enabled:
        email = _resolve_portal_email(customer, payload.email)
        _get_or_create_customer_user(db, customer, email)
        enabled = True
    else:
        # Disable ALL rows for the customer — with duplicates, deactivating
        # just one would leave another live with valid tokens.
        users = db.execute(
            select(CustomerUser).where(CustomerUser.customer_id == customer.id)
        ).scalars().all()
        for user in users:
            user.is_active = False
            user.portal_token = None
            user.portal_token_expires_at = None
        if users:
            db.commit()
        enabled = False

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=str(staff.get("sub") or staff.get("user_id") or "system"),
        action="portal_access_toggled",
        entity_type="customer",
        entity_id=str(customer.id),
        details={"portal_enabled": enabled},
    )
    db.commit()
    return {"ok": True, "portal_enabled": enabled}


@staff_router.post("/invite", response_model=None, dependencies=[Depends(require_permission("customers.write"))])
def portal_admin_invite(
    payload: PortalInviteIn,
    request: Request,
    staff: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    customer = db.execute(
        select(Customer).where(Customer.id == payload.customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    email = _resolve_portal_email(customer, payload.email)
    user = _get_or_create_customer_user(db, customer, email)

    token = secrets.token_urlsafe(32)
    user.portal_token = token
    user.portal_token_expires_at = _now_utc() + timedelta(days=INVITE_LINK_TTL_DAYS)
    db.commit()

    link = f"{_portal_link_base(request)}/customer-portal?token={token}"
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    staff_id = str(staff.get("sub") or staff.get("user_id") or "") or None
    sent, skip_reason = send_portal_magic_link_email(
        db,
        tenant_id,
        email,
        link,
        to_name=customer.name or "",
        sender_user_id=staff_id,
        expires_text=f"expires in {INVITE_LINK_TTL_DAYS} days",
    )

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=staff_id or "system",
        action="portal_invite_sent",
        entity_type="customer_user",
        entity_id=str(user.id),
        details={"email": email, "email_sent": bool(sent), "skip_reason": skip_reason},
    )
    db.commit()

    # magic_link is returned so staff can hand the link to the customer
    # directly (text, in person) when tenant email isn't configured.
    return {
        "ok": True,
        "invite_sent": bool(sent),
        "email": email,
        "magic_link": link,
        "email_skip_reason": skip_reason,
    }
