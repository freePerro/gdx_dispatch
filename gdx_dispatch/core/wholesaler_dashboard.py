from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text as _text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role

wholesaler_router = APIRouter(prefix="/dashboard", tags=["wholesaler"])

@wholesaler_router.get("", response_model=None, dependencies=[Depends(require_role("admin", "owner"))])
def get_wholesaler_dashboard(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    since = datetime.now(UTC) - timedelta(days=30)

    sku_count = 0
    active_sku_count = 0
    try:
        result = db.execute(
            _text("""
                SELECT COUNT(*) as sku_count,
                       SUM(CASE WHEN is_active THEN 1 ELSE 0 END) as active_sku_count
                FROM catalog_items WHERE wholesaler_tenant_id = :tenant_id
            """),
            {"tenant_id": tenant_id}
        ).fetchone()
        if result:
            sku_count = result.sku_count or 0
            active_sku_count = result.active_sku_count or 0
    except Exception:
        logging.getLogger(__name__).exception("get_wholesaler_dashboard caught exception")
        pass

    try:
        result = db.execute(
            _text("""
                SELECT active_distributors, total_channel_revenue
                FROM channel_analytics
                WHERE wholesaler_tenant_id = :tenant_id
                ORDER BY computed_at DESC LIMIT 1
            """),
            {"tenant_id": tenant_id}
        ).fetchone()
        if result:
            float(result.total_channel_revenue or 0)
    except Exception:
        logging.getLogger(__name__).exception("get_wholesaler_dashboard caught exception")
        pass

    orders_received_30d = 0
    revenue_30d = 0.0
    try:
        result = db.execute(
            _text("""
                SELECT COUNT(*) as orders_received_30d,
                       COALESCE(SUM(total_amount),0) as revenue_30d
                FROM dealer_orders WHERE created_at >= :since
            """),
            {"since": since}
        ).fetchone()
        if result:
            orders_received_30d = result.orders_received_30d or 0
            revenue_30d = float(result.revenue_30d or 0)
    except Exception:
        logging.getLogger(__name__).exception("get_wholesaler_dashboard caught exception")
        pass

    top_channels: list = []
    try:
        rows = db.execute(
            _text("""
                SELECT dealer_tenant_id as channel,
                       COUNT(*) as orders,
                       COALESCE(SUM(total_amount),0) as revenue
                FROM dealer_orders
                WHERE created_at >= :since
                GROUP BY dealer_tenant_id
                ORDER BY revenue DESC LIMIT 5
            """),
            {"since": since}
        ).fetchall()
        top_channels = [{"channel": row.channel, "orders": row.orders, "revenue": float(row.revenue)} for row in rows]
    except Exception:
        logging.getLogger(__name__).exception("get_wholesaler_dashboard caught exception")
        pass

    return JSONResponse(content=jsonable_encoder({
        "sku_count": sku_count,
        "active_sku_count": active_sku_count,
        "orders_received_30d": orders_received_30d,
        "revenue_30d": revenue_30d,
        "top_channels": top_channels,
    }))
