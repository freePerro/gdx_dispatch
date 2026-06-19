"""Supplier Invitation & Portal — dealers invite suppliers, suppliers manage catalogs & orders.

Flow:
1. Dealer sends invite → supplier gets email with link
2. Supplier clicks link → creates account
3. Supplier uploads/updates catalog → dealer sees live pricing
4. Dealer places order → supplier sees it and updates status
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    SupplierAccount,
    SupplierCatalogItem,
    SupplierInvitation,
    SupplierOrder,
    SupplierOrderLine,
    SupplierTenantLink,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["supplier-portal"])


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


# ── Schemas ──────────────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    supplier_email: str = Field(min_length=3, max_length=254)
    supplier_name: str = Field(min_length=1, max_length=200)


class SupplierRegisterRequest(BaseModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=8, max_length=128)
    company_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(default="", max_length=50)


class SupplierLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class CatalogItemIn(BaseModel):
    sku: str = Field(default="", max_length=100)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    unit_price: float = Field(ge=0)
    stock_level: int = Field(default=0, ge=0)
    category: str = Field(default="", max_length=100)


class OrderStatusUpdate(BaseModel):
    status: str = Field(pattern="^(confirmed|shipped|delivered|cancelled)$")


# ── Dealer endpoints (authed as tenant user) ─────────────────────────────────

@router.post("/api/supplier/invite", status_code=201,
             dependencies=[Depends(require_module("inventory"))])
def invite_supplier(
    body: InviteRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Dealer sends invite link to a supplier."""
    tid = _tid(request)
    token = secrets.token_urlsafe(32)
    invite_id = str(uuid4())
    base_url = os.getenv("SIGNUP_BASE_URL", "https://example.com")

    invitation = SupplierInvitation(
        id=invite_id,
        tenant_id=tid,
        supplier_email=body.supplier_email,
        supplier_name=body.supplier_name,
        token=token,
        status="pending",
    )
    db.add(invitation)
    db.commit()

    invite_url = f"{base_url}/supplier/join/{token}"

    log_audit_event_sync(db, tenant_id=tid, user_id=str(user.get("sub", "system")),
        action="supplier_invited", entity_type="supplier_invitation", entity_id=invite_id,
        details={"email": body.supplier_email, "name": body.supplier_name}, request=request)
    db.commit()

    return {"invite_id": invite_id, "token": token, "invite_url": invite_url}


@router.get("/api/supplier/invitations",
            dependencies=[Depends(require_module("inventory"))])
def list_invitations(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Dealer sees their sent invitations."""
    tid = _tid(request)
    stmt = (
        select(SupplierInvitation)
        .where(SupplierInvitation.tenant_id == tid)
        .order_by(SupplierInvitation.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return {"items": [
        {
            "id": str(r.id),
            "supplier_email": r.supplier_email,
            "supplier_name": r.supplier_name,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "accepted_at": r.accepted_at.isoformat() if r.accepted_at else None,
        }
        for r in rows
    ]}


# ── Public supplier endpoints (no tenant auth) ──────────────────────────────

@router.get("/supplier/join/{token}", response_class=HTMLResponse, include_in_schema=False)
def supplier_join_page(token: str, db: Session = Depends(get_db)) -> HTMLResponse:
    """Public page — supplier clicks invite link and sees registration form."""
    stmt = (
        select(SupplierInvitation)
        .where(SupplierInvitation.token == token, SupplierInvitation.status == "pending")
    )
    invite = db.execute(stmt).scalars().first()

    if not invite:
        return HTMLResponse(content="<h1>Invalid or expired invitation</h1>", status_code=404)

    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader
    tmpl_dir = Path(__file__).resolve().parent.parent / "templates" / "public"
    env = Environment(loader=FileSystemLoader(str(tmpl_dir)), autoescape=True)
    html = env.get_template("supplier_join.html").render(
        supplier_name=invite.supplier_name,
        supplier_email=invite.supplier_email,
        token=token,
    )
    return HTMLResponse(content=html)


@router.get("/supplier/join/{token}/form.js", include_in_schema=False)
def supplier_join_js(token: str) -> Response:
    """Serve the supplier join form handler JS (extracted for CSP compliance)."""
    from pathlib import Path
    js_path = Path(__file__).resolve().parent.parent / "static" / "supplier_join.js"
    return Response(content=js_path.read_text(), media_type="application/javascript")


@router.post("/api/supplier/register")
def register_supplier(body: SupplierRegisterRequest, db: Session = Depends(get_db)) -> dict:
    """Supplier creates account from invite token."""
    stmt = (
        select(SupplierInvitation)
        .where(SupplierInvitation.token == body.token, SupplierInvitation.status == "pending")
    )
    invite = db.execute(stmt).scalars().first()
    if not invite:
        raise HTTPException(status_code=400, detail="Invalid or expired invitation")

    # Check if account already exists
    existing = db.execute(
        select(SupplierAccount).where(SupplierAccount.email == invite.supplier_email)
    ).scalars().first()

    if existing:
        supplier_id = str(existing.id)
    else:
        supplier_id = str(uuid4())
        pw_hash = generate_password_hash(body.password)
        account = SupplierAccount(
            id=supplier_id,
            email=invite.supplier_email,
            password_hash=pw_hash,
            company_name=body.company_name,
            phone=body.phone or None,
        )
        db.add(account)

    # Link to tenant — check if link already exists
    existing_link = db.execute(
        select(SupplierTenantLink).where(
            SupplierTenantLink.supplier_id == supplier_id,
            SupplierTenantLink.tenant_id == str(invite.tenant_id),
        )
    ).scalars().first()
    if not existing_link:
        link = SupplierTenantLink(
            id=str(uuid4()),
            supplier_id=supplier_id,
            tenant_id=str(invite.tenant_id),
            status="active",
        )
        db.add(link)

    # Mark invite accepted
    now = datetime.now(timezone.utc)
    invite.status = "accepted"
    invite.accepted_at = now
    db.commit()

    return {"supplier_id": supplier_id, "email": invite.supplier_email, "company_name": body.company_name}


@router.post("/api/supplier/login")
def supplier_login(body: SupplierLoginRequest, db: Session = Depends(get_db)) -> dict:
    """Supplier authentication — returns supplier_id for session."""
    account = db.execute(
        select(SupplierAccount).where(SupplierAccount.email == body.email)
    ).scalars().first()

    if not account or not check_password_hash(account.password_hash, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Get linked tenants
    links = db.execute(
        select(SupplierTenantLink).where(
            SupplierTenantLink.supplier_id == account.id,
            SupplierTenantLink.status == "active",
        )
    ).scalars().all()

    return {
        "supplier_id": str(account.id),
        "company_name": account.company_name,
        "linked_tenants": [str(l.tenant_id) for l in links],
    }


# ── Supplier portal endpoints (supplier-authed) ─────────────────────────────

def _get_supplier_account(db: Session, supplier_id: str) -> SupplierAccount:
    """Helper to fetch a supplier account or raise 404.

    SupplierAccount.id is Uuid — psycopg casts bound params with ::UUID, so a
    non-UUID input raises DataError (500). Validate upstream so garbage input
    returns a clean 404 instead.
    """
    from uuid import UUID as _UUID
    try:
        _UUID(supplier_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Supplier not found")
    account = db.execute(
        select(SupplierAccount).where(SupplierAccount.id == supplier_id)
    ).scalars().first()
    if not account:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return account


@router.get("/api/supplier/portal/catalog")
def supplier_catalog(
    supplier_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict:
    """Supplier views/manages their catalog."""
    supplier = _get_supplier_account(db, supplier_id)
    items = db.execute(
        select(SupplierCatalogItem)
        .where(SupplierCatalogItem.supplier_name == supplier.company_name)
        .order_by(SupplierCatalogItem.category, SupplierCatalogItem.name)
    ).scalars().all()
    return {"items": [
        {
            "id": str(item.id),
            "sku": item.sku,
            "name": item.name,
            "description": item.description,
            "unit_price": float(item.unit_price),
            "stock_level": item.stock_level,
            "category": item.category,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in items
    ]}


@router.post("/api/supplier/portal/catalog", status_code=201)
def supplier_add_catalog_item(
    body: CatalogItemIn,
    supplier_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict:
    """Supplier adds or updates a catalog item."""
    supplier = _get_supplier_account(db, supplier_id)

    # Get all linked tenants
    links = db.execute(
        select(SupplierTenantLink).where(
            SupplierTenantLink.supplier_id == supplier.id,
            SupplierTenantLink.status == "active",
        )
    ).scalars().all()

    item_id = str(uuid4())
    for link in links:
        db.execute(text("""
            INSERT INTO supplier_catalog (id, company_id, supplier_name, sku, name, description, unit_price, stock_level, category)
            VALUES (:id, :tid, :sn, :sku, :name, :desc, :price, :stock, :cat)
            ON CONFLICT DO NOTHING
        """), {
            "id": str(uuid4()), "tid": str(link.tenant_id), "sn": supplier.company_name,
            "sku": body.sku, "name": body.name, "desc": body.description,
            "price": body.unit_price, "stock": body.stock_level, "cat": body.category,
        })

    db.commit()
    return {"id": item_id, "name": body.name, "tenants_updated": len(links)}


@router.get("/api/supplier/portal/orders")
def supplier_orders(
    supplier_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict:
    """Supplier sees incoming orders from all linked tenants."""
    supplier = _get_supplier_account(db, supplier_id)

    orders = db.execute(
        select(SupplierOrder)
        .where(SupplierOrder.supplier_name == supplier.company_name)
        .order_by(SupplierOrder.created_at.desc())
    ).scalars().all()
    return {"items": [
        {
            "id": str(o.id),
            "company_id": o.company_id,
            "supplier_name": o.supplier_name,
            "status": o.status,
            "total_amount": float(o.total_amount),
            "notes": o.notes,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in orders
    ]}


@router.get("/api/supplier/portal/delivery-loadsheet")
def delivery_loadsheet(
    supplier_id: str = Query(..., min_length=1),
    date: str = Query(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """Delivery load sheet — checklist of everything for each stop on the truck.

    Groups all pending/confirmed orders by dealer, shows items per stop
    so warehouse crew can load the truck in delivery order.
    """
    from datetime import date as _date_type

    if not date:
        date = _date_type.today().isoformat()

    supplier = _get_supplier_account(db, supplier_id)

    # Get all pending/confirmed orders for this supplier via ORM
    pending_orders = db.execute(
        select(SupplierOrder)
        .where(
            SupplierOrder.supplier_name == supplier.company_name,
            SupplierOrder.status.in_(["pending", "confirmed"]),
        )
        .order_by(SupplierOrder.created_at)
    ).scalars().all()

    stops = []
    total_items = 0
    total_qty = 0

    for order in pending_orders:
        # Get dealer info from tenant — use company_id as dealer reference
        dealer_name = order.company_id
        dealer_address = ""

        # Get order line items via ORM (avoids CAST(order_id AS TEXT) portability bug)
        lines = db.execute(
            select(SupplierOrderLine)
            .where(SupplierOrderLine.order_id == order.id)
            .order_by(SupplierOrderLine.name)
        ).scalars().all()

        items = []
        for line in lines:
            qty = int(line.quantity or 1)
            items.append({
                "sku": line.sku or "",
                "name": line.name,
                "quantity": qty,
                "unit_price": float(line.unit_price or 0),
                "checked": False,
            })
            total_qty += qty

        total_items += len(items)

        stops.append({
            "order_id": str(order.id),
            "dealer": dealer_name,
            "address": dealer_address,
            "status": order.status,
            "notes": order.notes or "",
            "total_amount": float(order.total_amount or 0),
            "items": items,
            "item_count": len(items),
        })

    return {
        "date": date,
        "supplier_name": supplier.company_name,
        "stops": stops,
        "total_stops": len(stops),
        "total_items": total_items,
        "total_qty": total_qty,
    }


@router.patch("/api/supplier/portal/orders/{order_id}")
def supplier_update_order(
    order_id: str,
    body: OrderStatusUpdate,
    supplier_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict:
    """Supplier updates order status (confirmed, shipped, delivered, cancelled)."""
    supplier = _get_supplier_account(db, supplier_id)
    now = datetime.now(timezone.utc)

    # Use ORM to avoid CAST(id AS TEXT) portability bug
    from uuid import UUID as _UUID
    try:
        order_uuid = _UUID(order_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Order not found") from None
    matched = db.execute(
        select(SupplierOrder).where(
            SupplierOrder.id == order_uuid,
            SupplierOrder.supplier_name == supplier.company_name,
        )
    ).scalars().first()
    if matched is None:
        raise HTTPException(status_code=404, detail="Order not found")

    matched.status = body.status
    matched.updated_at = now
    db.commit()
    return {"order_id": order_id, "status": body.status}
