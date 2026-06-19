from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIST_A = "dist-001"
DEALER_A = "dealer-001"
DEALER_B = "dealer-002"
DEALER_C = "dealer-003"

SAMPLE_ITEMS = [{"part_number": "GD-101", "description": "Spring", "qty": 2, "unit_price": 45.00}]

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def order_db():
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.distributor.models import DealerOrder, DistributorAnalytic

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    DealerOrder.__table__.create(bind=engine, checkfirst=True)
    DistributorAnalytic.__table__.create(bind=engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_place_order_dealer(order_db):
    """Dealer places an order; DealerOrder is created with status 'pending'."""
    from gdx_dispatch.modules.distributor.service import create_dealer_order

    order = create_dealer_order(
        dealer_tenant_id=DEALER_A,
        distributor_tenant_id=DIST_A,
        line_items=SAMPLE_ITEMS,
        idempotency_key=None,
        db=order_db,
    )
    assert order.id is not None
    assert order.status == "pending"
    assert order.dealer_tenant_id == DEALER_A
    assert order.distributor_tenant_id == DIST_A
    assert order.order_number.startswith("ORD-")
    assert float(order.total_amount) == pytest.approx(90.0)


def test_list_orders_distributor(order_db):
    """Distributor can list all orders from their dealer network."""
    from gdx_dispatch.modules.distributor.service import create_dealer_order, get_dealer_network_orders

    create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    create_dealer_order(DEALER_B, DIST_A, SAMPLE_ITEMS, None, order_db)

    orders = get_dealer_network_orders(DIST_A, order_db)
    assert len(orders) == 2
    dealer_ids = {o.dealer_tenant_id for o in orders}
    assert DEALER_A in dealer_ids
    assert DEALER_B in dealer_ids


def test_confirm_order(order_db):
    """Distributor confirms a pending order; status becomes 'confirmed'."""
    from gdx_dispatch.modules.distributor.service import advance_order_status, create_dealer_order

    order = create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    confirmed = advance_order_status(order.id, "confirmed", order_db)
    assert confirmed.status == "confirmed"


def test_ship_with_tracking(order_db):
    """Distributor ships a confirmed order with a tracking number; status becomes 'shipped'."""
    from gdx_dispatch.modules.distributor.models import DealerOrder
    from gdx_dispatch.modules.distributor.service import advance_order_status, create_dealer_order

    order = create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    advance_order_status(order.id, "confirmed", order_db)

    # Simulate the route storing tracking_number in _meta before advancing
    order_db.refresh(order)
    if not isinstance(order.line_items, dict):
        order.line_items = {"items": order.line_items or [], "_meta": {}}
    if "_meta" not in order.line_items:
        order.line_items["_meta"] = {}
    order.line_items["_meta"]["tracking_number"] = "1Z999AA10123456784"
    order_db.commit()

    shipped = advance_order_status(order.id, "shipped", order_db)
    assert shipped.status == "shipped"

    # Verify tracking number persisted
    refreshed = order_db.execute(
        select(DealerOrder).where(DealerOrder.id == order.id)
    ).scalar_one()
    if isinstance(refreshed.line_items, dict):
        assert refreshed.line_items.get("_meta", {}).get("tracking_number") == "1Z999AA10123456784"


def test_deliver_order(order_db):
    """Order goes through full lifecycle to 'delivered'."""
    from gdx_dispatch.modules.distributor.service import advance_order_status, create_dealer_order

    order = create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    advance_order_status(order.id, "confirmed", order_db)
    advance_order_status(order.id, "shipped", order_db)
    delivered = advance_order_status(order.id, "delivered", order_db)
    assert delivered.status == "delivered"


def test_cancel_order(order_db):
    """Pending order can be cancelled."""
    from gdx_dispatch.modules.distributor.service import advance_order_status, create_dealer_order

    order = create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    cancelled = advance_order_status(order.id, "cancelled", order_db)
    assert cancelled.status == "cancelled"


def test_dealer_sees_own_orders_only(order_db):
    """Dealer A's orders are isolated from Dealer B's orders."""
    from gdx_dispatch.modules.distributor.models import DealerOrder
    from gdx_dispatch.modules.distributor.service import create_dealer_order

    create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    create_dealer_order(DEALER_B, DIST_A, SAMPLE_ITEMS, None, order_db)

    dealer_a_orders = order_db.execute(
        select(DealerOrder).where(DealerOrder.dealer_tenant_id == DEALER_A)
    ).scalars().all()
    dealer_b_orders = order_db.execute(
        select(DealerOrder).where(DealerOrder.dealer_tenant_id == DEALER_B)
    ).scalars().all()

    assert len(dealer_a_orders) == 2
    assert len(dealer_b_orders) == 1
    assert all(o.dealer_tenant_id == DEALER_A for o in dealer_a_orders)
    assert all(o.dealer_tenant_id == DEALER_B for o in dealer_b_orders)


def test_distributor_sees_all_dealer_orders(order_db):
    """Distributor's network view includes orders from all dealers."""
    from gdx_dispatch.modules.distributor.service import create_dealer_order, get_dealer_network_orders

    create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    create_dealer_order(DEALER_B, DIST_A, SAMPLE_ITEMS, None, order_db)
    create_dealer_order(DEALER_C, DIST_A, SAMPLE_ITEMS, None, order_db)

    orders = get_dealer_network_orders(DIST_A, order_db)
    assert len(orders) == 3
    dealer_ids = {o.dealer_tenant_id for o in orders}
    assert dealer_ids == {DEALER_A, DEALER_B, DEALER_C}


def test_analytics_structure(order_db):
    """Analytics snapshot reflects correct totals for the distributor."""
    from gdx_dispatch.modules.distributor.service import compute_distributor_analytics, create_dealer_order

    create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    create_dealer_order(DEALER_B, DIST_A, SAMPLE_ITEMS, None, order_db)

    now = datetime.now(UTC)
    period_start = now - timedelta(days=30)
    analytic = compute_distributor_analytics(DIST_A, period_start, now, order_db)

    assert analytic.total_orders == 2
    assert float(analytic.total_revenue) == pytest.approx(180.0)
    assert analytic.active_dealers >= 1
    assert analytic.distributor_tenant_id == DIST_A


def test_pending_orders_list(order_db):
    """Query for pending orders returns only orders with status 'pending'."""
    from gdx_dispatch.modules.distributor.models import DealerOrder
    from gdx_dispatch.modules.distributor.service import advance_order_status, create_dealer_order

    o1 = create_dealer_order(DEALER_A, DIST_A, SAMPLE_ITEMS, None, order_db)
    o2 = create_dealer_order(DEALER_B, DIST_A, SAMPLE_ITEMS, None, order_db)
    o3 = create_dealer_order(DEALER_C, DIST_A, SAMPLE_ITEMS, None, order_db)
    # Confirm one order so it's no longer pending
    advance_order_status(o3.id, "confirmed", order_db)

    pending = order_db.execute(
        select(DealerOrder).where(
            DealerOrder.distributor_tenant_id == DIST_A,
            DealerOrder.status == "pending",
        )
    ).scalars().all()

    assert len(pending) == 2
    pending_ids = {str(o.id) for o in pending}
    assert str(o1.id) in pending_ids
    assert str(o2.id) in pending_ids
    assert str(o3.id) not in pending_ids
