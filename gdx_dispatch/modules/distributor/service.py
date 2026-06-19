from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.distributor.models import DealerOrder, DistributorAnalytic


def create_dealer_order(
    dealer_tenant_id: str,
    distributor_tenant_id: str,
    line_items: list[dict],
    idempotency_key: str | None,
    db: Session,
) -> DealerOrder:
    # Check idempotency
    if idempotency_key:
        existing = db.execute(
            select(DealerOrder).where(DealerOrder.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing:
            return existing

    total = sum(item.get("qty", 1) * item.get("unit_price", 0) for item in line_items)
    order = DealerOrder(
        dealer_tenant_id=dealer_tenant_id,
        distributor_tenant_id=distributor_tenant_id,
        order_number=f"ORD-{uuid4().hex[:8].upper()}",
        line_items=line_items,
        total_amount=total,
        idempotency_key=idempotency_key,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def advance_order_status(order_id, new_status: str, db: Session) -> DealerOrder:
    valid_transitions = {
        "pending": {"confirmed", "cancelled"},
        "confirmed": {"shipped", "cancelled"},
        "shipped": {"delivered"},
        "delivered": set(),
        "cancelled": set(),
    }
    order = db.execute(select(DealerOrder).where(DealerOrder.id == order_id)).scalar_one_or_none()
    if not order:
        raise ValueError("Order not found")
    allowed = valid_transitions.get(order.status, set())
    if new_status not in allowed:
        raise ValueError(f"Invalid transition: {order.status} -> {new_status}. Allowed: {sorted(allowed)}")
    order.status = new_status
    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return order


def get_dealer_network_orders(distributor_tenant_id: str, db: Session) -> list[DealerOrder]:
    return list(
        db.execute(
            select(DealerOrder)
            .where(DealerOrder.distributor_tenant_id == distributor_tenant_id)
            .order_by(DealerOrder.created_at.desc())
        ).scalars().all()
    )


def compute_distributor_analytics(
    distributor_tenant_id: str,
    period_start: datetime,
    period_end: datetime,
    db: Session,
) -> DistributorAnalytic:
    orders = db.execute(
        select(DealerOrder).where(
            DealerOrder.distributor_tenant_id == distributor_tenant_id,
            DealerOrder.created_at >= period_start,
            DealerOrder.created_at < period_end,
            DealerOrder.status != "cancelled",
        )
    ).scalars().all()

    total_orders = len(orders)
    total_revenue = sum(float(o.total_amount) for o in orders)
    active_dealers = len({o.dealer_tenant_id for o in orders})

    analytic = DistributorAnalytic(
        distributor_tenant_id=distributor_tenant_id,
        period_start=period_start,
        period_end=period_end,
        active_dealers=active_dealers,
        total_orders=total_orders,
        total_revenue=total_revenue,
    )
    db.add(analytic)
    db.commit()
    db.refresh(analytic)
    return analytic
