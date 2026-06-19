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

distributor_router = APIRouter(prefix="/dashboard", tags=["distributor"])

@distributor_router.get("", response_model=None, dependencies=[Depends(require_role("admin", "owner"))])
def get_distributor_dashboard(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    since = datetime.now(UTC) - timedelta(days=30)

    try:
        result = db.execute(
            _text("""
                SELECT COUNT(*) as total_orders_30d,
                       COALESCE(SUM(total_amount),0) as total_revenue_30d,
                       COUNT(DISTINCT dealer_tenant_id) as dealer_count
                FROM dealer_orders
                WHERE distributor_tenant_id = :tenant_id AND created_at >= :since
            """),
            {"tenant_id": tenant_id, "since": since}
        )
        row = result.fetchone()
        total_orders_30d = row.total_orders_30d if row else 0
        total_revenue_30d = row.total_revenue_30d if row else 0
        dealer_count = row.dealer_count if row else 0
    except Exception:
        logging.getLogger(__name__).exception("get_distributor_dashboard caught exception")
        total_orders_30d = 0
        total_revenue_30d = 0
        dealer_count = 0

    try:
        result = db.execute(
            _text("""
                SELECT active_dealers
                FROM distributor_analytics
                WHERE distributor_tenant_id = :tenant_id
                ORDER BY computed_at DESC LIMIT 1
            """),
            {"tenant_id": tenant_id}
        )
        row = result.fetchone()
        active_dealer_count = row.active_dealers if row else 0
    except Exception:
        logging.getLogger(__name__).exception("get_distributor_dashboard caught exception")
        active_dealer_count = 0

    try:
        result = db.execute(
            _text("""
                SELECT dealer_tenant_id,
                       COUNT(*) as orders,
                       COALESCE(SUM(total_amount),0) as revenue
                FROM dealer_orders
                WHERE distributor_tenant_id = :tenant_id AND created_at >= :since
                GROUP BY dealer_tenant_id
                ORDER BY revenue DESC LIMIT 5
            """),
            {"tenant_id": tenant_id, "since": since}
        )
        top_dealers = [dict(row._mapping) for row in result.fetchall()]
    except Exception:
        logging.getLogger(__name__).exception("get_distributor_dashboard caught exception")
        top_dealers = []

    return JSONResponse(content=jsonable_encoder({
        "dealer_count": dealer_count,
        "active_dealer_count": active_dealer_count,
        "total_orders_30d": total_orders_30d,
        "total_revenue_30d": total_revenue_30d,
        "top_dealers": top_dealers,
    }))
