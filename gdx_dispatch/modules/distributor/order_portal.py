from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.modules.distributor.models import DealerOrder
from gdx_dispatch.modules.distributor.service import (
    advance_order_status,
    compute_distributor_analytics,
    create_dealer_order,
)

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ShipRequest(BaseModel):
    tracking_number: str


class CancelRequest(BaseModel):
    reason: str = ""


class PlaceOrderRequest(BaseModel):
    distributor_tenant_id: str
    items: list[dict]
    dealer_po_number: str = ""
    shipping_address: dict = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _order_dict(order: DealerOrder) -> dict:
    return {
        "id": str(order.id),
        "order_number": order.order_number,
        "dealer_tenant_id": order.dealer_tenant_id,
        "distributor_tenant_id": order.distributor_tenant_id,
        "status": order.status,
        "line_items": order.line_items,
        "total_amount": float(order.total_amount),
        "idempotency_key": order.idempotency_key,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        logging.getLogger(__name__).exception("_parse_date caught exception")
        return None


def _get_order_or_404(order_id: str, db: Session) -> DealerOrder:
    try:
        oid = UUID(order_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Order not found") from None
    order = db.execute(
        select(DealerOrder).where(DealerOrder.id == oid)
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def _ensure_meta(order: DealerOrder) -> dict:
    """Ensure line_items is a dict and return the _meta sub-dict."""
    if not isinstance(order.line_items, dict):
        order.line_items = {"items": order.line_items or [], "_meta": {}}
    if "_meta" not in order.line_items:
        order.line_items["_meta"] = {}
    return order.line_items["_meta"]


# ---------------------------------------------------------------------------
# Distributor router — full order management
# ---------------------------------------------------------------------------

distributor_router = APIRouter(prefix="/api/distributor", tags=["distributor-orders"])


@distributor_router.get(
    "/orders",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def list_distributor_orders(
    request: Request,
    dealer_id: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    stmt = select(DealerOrder).where(DealerOrder.distributor_tenant_id == tenant_id)
    if dealer_id:
        stmt = stmt.where(DealerOrder.dealer_tenant_id == dealer_id)
    if status:
        stmt = stmt.where(DealerOrder.status == status)
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to)
    if dt_from:
        stmt = stmt.where(DealerOrder.created_at >= dt_from)
    if dt_to:
        stmt = stmt.where(DealerOrder.created_at <= dt_to)
    stmt = stmt.order_by(DealerOrder.created_at.desc())
    orders = db.execute(stmt).scalars().all()
    return JSONResponse(content=jsonable_encoder([_order_dict(o) for o in orders]))


@distributor_router.get(
    "/orders/pending",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def list_pending_orders(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    orders = db.execute(
        select(DealerOrder)
        .where(
            DealerOrder.distributor_tenant_id == tenant_id,
            DealerOrder.status == "pending",
        )
        .order_by(DealerOrder.created_at.asc())
    ).scalars().all()
    return JSONResponse(content=jsonable_encoder([_order_dict(o) for o in orders]))


@distributor_router.get(
    "/orders/{order_id}",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def get_distributor_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    order = _get_order_or_404(order_id, db)
    if order.distributor_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=jsonable_encoder(_order_dict(order)))


@distributor_router.post(
    "/orders/{order_id}/confirm",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def confirm_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    order = _get_order_or_404(order_id, db)
    if order.distributor_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        order = advance_order_status(order.id, "confirmed", db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(content=jsonable_encoder(_order_dict(order)))


@distributor_router.post(
    "/orders/{order_id}/ship",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def ship_order(
    order_id: str,
    body: ShipRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    order = _get_order_or_404(order_id, db)
    if order.distributor_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    meta = _ensure_meta(order)
    meta["tracking_number"] = body.tracking_number
    try:
        order = advance_order_status(order.id, "shipped", db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(content=jsonable_encoder(_order_dict(order)))


@distributor_router.post(
    "/orders/{order_id}/deliver",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def deliver_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    order = _get_order_or_404(order_id, db)
    if order.distributor_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        order = advance_order_status(order.id, "delivered", db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(content=jsonable_encoder(_order_dict(order)))


@distributor_router.post(
    "/orders/{order_id}/cancel",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def cancel_order(
    order_id: str,
    body: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    order = _get_order_or_404(order_id, db)
    if order.distributor_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    if body.reason:
        meta = _ensure_meta(order)
        meta["cancel_reason"] = body.reason
    try:
        order = advance_order_status(order.id, "cancelled", db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(content=jsonable_encoder(_order_dict(order)))


@distributor_router.get(
    "/analytics/orders",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def order_analytics(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    period_start = now - timedelta(days=30)

    try:
        analytic = compute_distributor_analytics(tenant_id, period_start, now, db)
        total_30d = analytic.total_orders
        revenue_30d = float(analytic.total_revenue)
        avg_order_value = (revenue_30d / total_30d) if total_30d else 0.0
    except Exception:
        logging.getLogger(__name__).exception("order_analytics caught exception")
        total_30d = 0
        revenue_30d = 0.0
        avg_order_value = 0.0

    # Top dealers by revenue in last 30 days
    try:
        rows = db.execute(
            select(
                DealerOrder.dealer_tenant_id,
                func.count(DealerOrder.id).label("order_count"),
                func.coalesce(func.sum(DealerOrder.total_amount), 0).label("revenue"),
            )
            .where(
                DealerOrder.distributor_tenant_id == tenant_id,
                DealerOrder.created_at >= period_start,
                DealerOrder.status != "cancelled",
            )
            .group_by(DealerOrder.dealer_tenant_id)
            .order_by(func.sum(DealerOrder.total_amount).desc())
            .limit(5)
        ).all()
        top_dealers = [
            {
                "dealer_tenant_id": row.dealer_tenant_id,
                "order_count": row.order_count,
                "revenue": float(row.revenue),
            }
            for row in rows
        ]
    except Exception:
        logging.getLogger(__name__).exception("order_analytics caught exception")
        top_dealers = []

    return JSONResponse(
        content=jsonable_encoder(
            {
                "total_30d": total_30d,
                "revenue_30d": revenue_30d,
                "avg_order_value": avg_order_value,
                "top_dealers": top_dealers,
            }
        )
    )


# ---------------------------------------------------------------------------
# Dealer router — order placement and status tracking
# ---------------------------------------------------------------------------

dealer_router = APIRouter(prefix="/api/dealer", tags=["dealer-orders"])


@dealer_router.post(
    "/orders",
    dependencies=[Depends(require_role("admin", "owner", "technician"))],
    status_code=201,
)
def place_order(
    body: PlaceOrderRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    dealer_tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))

    # Embed dealer_po_number and shipping_address into items list as _meta
    items: list[dict] = list(body.items)
    meta: dict = {}
    if body.dealer_po_number:
        meta["dealer_po_number"] = body.dealer_po_number
    if body.shipping_address:
        meta["shipping_address"] = body.shipping_address
    line_items: dict | list = {"items": items, "_meta": meta} if meta else items

    try:
        order = create_dealer_order(
            dealer_tenant_id=dealer_tenant_id,
            distributor_tenant_id=body.distributor_tenant_id,
            line_items=line_items,
            idempotency_key=idempotency_key,
            db=db,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return JSONResponse(status_code=201, content=jsonable_encoder(_order_dict(order)))


@dealer_router.get(
    "/orders",
    dependencies=[Depends(require_role("admin", "owner", "technician"))],
)
def list_dealer_orders(
    request: Request,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
) -> JSONResponse:
    dealer_tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    stmt = select(DealerOrder).where(DealerOrder.dealer_tenant_id == dealer_tenant_id)
    if status:
        stmt = stmt.where(DealerOrder.status == status)
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to)
    if dt_from:
        stmt = stmt.where(DealerOrder.created_at >= dt_from)
    if dt_to:
        stmt = stmt.where(DealerOrder.created_at <= dt_to)
    stmt = stmt.order_by(DealerOrder.created_at.desc())
    orders = db.execute(stmt).scalars().all()
    return JSONResponse(content=jsonable_encoder([_order_dict(o) for o in orders]))


@dealer_router.get(
    "/orders/{order_id}",
    dependencies=[Depends(require_role("admin", "owner", "technician"))],
)
def get_dealer_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    dealer_tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    order = _get_order_or_404(order_id, db)
    if order.dealer_tenant_id != dealer_tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(content=jsonable_encoder(_order_dict(order)))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

router = distributor_router
