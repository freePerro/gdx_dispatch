"""D-S122-change-orders-create-flow — line items round-trip.

Pins:
- POST /api/change-orders with line_items writes ChangeOrderLine rows
- amount auto-derives from line subtotal
- GET /api/change-orders/{id} returns line_items
- Empty line_items array doesn't break the legacy bare-amount path
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — register ChangeOrderLine
from gdx_dispatch.models.tenant_models import ChangeOrderLine
from gdx_dispatch.routers.change_orders import (
    ChangeOrder,
    ChangeOrderIn,
    create_change_order,
    get_change_order,
    update_change_order,
)
from uuid import UUID


@pytest.fixture
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=eng, checkfirst=True)
    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _user() -> dict:
    return {"sub": "user-1", "email": "test@example.com", "role": "admin"}


def test_create_change_order_with_line_items_persists_lines(db):
    payload = ChangeOrderIn(
        title="Outlet install",
        line_items=[
            {"description": "Outlet box", "quantity": 1, "unit_price": 25.0},
            {"description": "Labor 1hr", "quantity": 1, "unit_price": 95.0},
        ],
    )
    out = create_change_order(payload=payload, user=_user(), db=db)

    assert out["amount"] == 120.0  # 25 + 95
    assert out["co_number"].startswith("CO-")

    co_id = UUID(out["id"])
    lines = db.execute(
        select(ChangeOrderLine).where(ChangeOrderLine.co_id == co_id)
    ).scalars().all()
    assert len(lines) == 2
    assert {(ln.description, float(ln.unit_price)) for ln in lines} == {
        ("Outlet box", 25.0),
        ("Labor 1hr", 95.0),
    }


def test_get_change_order_returns_line_items(db):
    out = create_change_order(
        payload=ChangeOrderIn(
            title="Scope add",
            line_items=[
                {"description": "Wiring", "quantity": 1, "unit_price": 200.0},
            ],
        ),
        user=_user(), db=db,
    )
    detail = get_change_order(co_id=UUID(out["id"]), _=_user(), db=db)
    assert "line_items" in detail
    assert len(detail["line_items"]) == 1
    assert detail["line_items"][0]["description"] == "Wiring"
    assert detail["line_items"][0]["unit_price"] == 200.0
    assert detail["line_items"][0]["line_total"] == 200.0


def test_create_change_order_without_line_items_uses_flat_amount(db):
    payload = ChangeOrderIn(title="Legacy", amount=300.0)
    out = create_change_order(payload=payload, user=_user(), db=db)
    assert out["amount"] == 300.0

    co_id = UUID(out["id"])
    lines = db.execute(
        select(ChangeOrderLine).where(ChangeOrderLine.co_id == co_id)
    ).scalars().all()
    assert lines == []


def test_create_change_order_qty_multiplies_into_amount(db):
    payload = ChangeOrderIn(
        title="Bulk",
        line_items=[
            {"description": "Spring", "quantity": 4, "unit_price": 50.0},
        ],
    )
    out = create_change_order(payload=payload, user=_user(), db=db)
    assert out["amount"] == 200.0  # 4 × 50


def test_patch_change_order_replaces_lines(db):
    """Auditor round-2 catch: PATCH was silently dropping line_items, so
    the edit-with-lines flow blanked the line set + set amount=0. Now PATCH
    replaces ChangeOrderLine rows and recomputes the amount from the new
    subtotal."""
    out = create_change_order(
        payload=ChangeOrderIn(
            title="Initial",
            line_items=[
                {"description": "Original A", "quantity": 1, "unit_price": 10.0},
                {"description": "Original B", "quantity": 1, "unit_price": 20.0},
            ],
        ),
        user=_user(), db=db,
    )
    co_id = UUID(out["id"])
    assert out["amount"] == 30.0

    # Edit: replace lines with a new set.
    updated = update_change_order(
        co_id=co_id,
        payload=ChangeOrderIn(
            title="Initial",
            line_items=[
                {"description": "Replacement", "quantity": 2, "unit_price": 75.0},
            ],
        ),
        _=_user(), db=db,
    )
    # Amount recomputed from new lines (2 × 75 = 150), original lines gone.
    assert updated["amount"] == 150.0
    detail = get_change_order(co_id=co_id, _=_user(), db=db)
    assert len(detail["line_items"]) == 1
    assert detail["line_items"][0]["description"] == "Replacement"


def test_patch_change_order_without_line_items_preserves_existing(db):
    """PATCH that omits line_items entirely should leave the existing lines
    untouched (legacy bare-amount edit path)."""
    out = create_change_order(
        payload=ChangeOrderIn(
            title="Keep me",
            line_items=[
                {"description": "Sticky line", "quantity": 1, "unit_price": 99.0},
            ],
        ),
        user=_user(), db=db,
    )
    co_id = UUID(out["id"])
    # PATCH with NO line_items (and no amount-override either).
    update_change_order(
        co_id=co_id,
        payload=ChangeOrderIn(title="Keep me", reason="other"),
        _=_user(), db=db,
    )
    detail = get_change_order(co_id=co_id, _=_user(), db=db)
    assert len(detail["line_items"]) == 1
    assert detail["line_items"][0]["description"] == "Sticky line"
    assert detail["amount"] == 99.0  # unchanged


