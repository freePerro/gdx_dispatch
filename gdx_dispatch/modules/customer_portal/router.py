from __future__ import annotations

import logging
import os
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Invoice, Job
from gdx_dispatch.modules.customer_portal.auth import send_magic_link, verify_magic_link
from gdx_dispatch.modules.customer_portal.models import CustomerUser

router = APIRouter(prefix="/portal", tags=["customer_portal"], dependencies=[Depends(require_module("customer_portal"))])

class MagicLinkIn(BaseModel): email: str  # noqa: E701,E702

def _current_portal_user(request: Request, db: Session = Depends(get_db)) -> CustomerUser:
    user_id = request.cookies.get("customer_portal_user_id")
    user = db.execute(select(CustomerUser).where(CustomerUser.id == user_id, CustomerUser.is_active.is_(True))).scalar_one_or_none() if user_id else None
    if not user: raise HTTPException(status_code=401, detail="Customer portal authentication required")  # noqa: E701,E702
    return user

@router.get("", response_model=None)
def portal_home(user: CustomerUser = Depends(_current_portal_user)) -> dict[str, str]:
    return {"status": "ok", "customer_id": str(user.customer_id)}

@router.get("/jobs", response_model=None)
def portal_jobs(user: CustomerUser = Depends(_current_portal_user), db: Session = Depends(get_db)) -> list[Job]:
    return list(db.execute(select(Job).where(Job.customer_id == user.customer_id, Job.deleted_at.is_(None)).order_by(Job.created_at.desc())).scalars().all())

@router.get("/invoices", response_model=None)
def portal_invoices(user: CustomerUser = Depends(_current_portal_user), db: Session = Depends(get_db)) -> list[Invoice]:
    q = select(Invoice).join(Job, Invoice.job_id == Job.id).where(Job.customer_id == user.customer_id, Invoice.deleted_at.is_(None)).order_by(Invoice.created_at.desc())
    return list(db.execute(q).scalars().all())

@router.post("/invoices/{invoice_id}/pay", response_model=None)
def pay_invoice(invoice_id: UUID, request: Request, user: CustomerUser = Depends(_current_portal_user), db: Session = Depends(get_db)) -> RedirectResponse:
    inv = db.execute(select(Invoice).join(Job, Invoice.job_id == Job.id).where(Invoice.id == invoice_id, Job.customer_id == user.customer_id, Invoice.deleted_at.is_(None))).scalar_one_or_none()
    if not inv: raise HTTPException(status_code=404, detail="Invoice not found")  # noqa: E701,E702
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    s = stripe.checkout.Session.create(mode="payment", line_items=[{"price_data": {"currency": "usd", "product_data": {"name": f"Invoice {inv.invoice_number}"}, "unit_amount": int(float(inv.total) * 100)}, "quantity": 1}], success_url=str(request.url_for("public_invoice", public_token=inv.public_token)), cancel_url=str(request.url_for("public_invoice", public_token=inv.public_token)))
    return RedirectResponse(s.url, status_code=303)

@router.get("/invoices/{public_token}", response_model=None, name="public_invoice")
def public_invoice(public_token: str, db: Session = Depends(get_db)) -> Invoice:
    from datetime import datetime, timedelta, timezone
    TOKEN_MAX_AGE_DAYS = int(os.getenv("PUBLIC_TOKEN_MAX_AGE_DAYS", "90"))
    inv = db.execute(select(Invoice).where(Invoice.public_token == public_token, Invoice.deleted_at.is_(None))).scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    # Check token age — expire after configurable days
    if TOKEN_MAX_AGE_DAYS > 0 and inv.created_at:
        created = inv.created_at if hasattr(inv.created_at, 'tzinfo') else inv.created_at
        cutoff = datetime.now(timezone.utc) - timedelta(days=TOKEN_MAX_AGE_DAYS)
        try:
            if created.replace(tzinfo=timezone.utc) < cutoff:
                raise HTTPException(status_code=410, detail="This link has expired")
        except (TypeError, AttributeError):
            logging.getLogger(__name__).exception("public_invoice caught exception")
            pass  # If created_at is not a datetime, skip expiry check
    return inv

@router.post("/auth/magic-link", response_model=None)
def request_magic_link(payload: MagicLinkIn, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.execute(select(CustomerUser).where(CustomerUser.email == payload.email, CustomerUser.is_active.is_(True))).scalar_one_or_none()
    return {"magic_link": send_magic_link(payload.email, user.customer_id, db) if user else ""}

@router.get("/auth/verify/{token}", response_model=None)
def verify_portal_link(token: str, db: Session = Depends(get_db)) -> RedirectResponse:
    user = verify_magic_link(token, db)
    if not user: raise HTTPException(status_code=400, detail="Invalid or expired magic link")  # noqa: E701,E702
    resp = RedirectResponse("/portal", status_code=303)
    resp.set_cookie("customer_portal_user_id", str(user.id), httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp
