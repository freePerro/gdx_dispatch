"""PATCH /api/inventory/parts applies only what the client sent (GDXA-1578).

``InventoryItemIn`` gives quantity, reorder_level, unit_cost and unit_price
create-time defaults (0, 5, 0, 0). ``update_part`` used to write them whenever
they were not None — always — so InventoryView, which has no price field and
never sends ``unit_price``, zeroed the price on every edit. Its audit rows were
empty too: ``create_part`` wrote ``entity_id=""`` and every handler wrote
``details={}`` with no tenant, because none of them took the request.

Through the real router; each audit assertion reads ``audit_logs`` back.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import InventoryItem
from gdx_dispatch.routers import inventory as inventory_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"


@pytest.fixture
def client_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    app = FastAPI()
    app.include_router(inventory_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER, "tenant_id": TENANT, "role": "admin",
    }
    app.dependency_overrides[require_module("inventory")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.user = {"user_id": USER, "tenant_id": TENANT}
        return await call_next(request)

    yield TestClient(app), db
    db.close()
    engine.dispose()


def _audit(db, action: str) -> AuditLog:
    db.expire_all()
    (row,) = db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
    return row


def _create(client) -> str:
    r = client.post("/api/inventory/parts", json={
        "part_name": "Torsion spring", "sku": "TS-207", "quantity": 12,
        "reorder_level": 3, "unit_cost": 18.5, "unit_price": 42.0,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item(db, item_id: str) -> InventoryItem:
    db.expire_all()
    return db.get(InventoryItem, UUID(item_id))


def test_patch_with_only_part_name_leaves_numbers_unchanged(client_and_db):
    client, db = client_and_db
    item_id = _create(client)

    r = client.patch(f"/api/inventory/parts/{item_id}", json={"part_name": "Torsion spring 2in"})
    assert r.status_code == 200, r.text

    item = _item(db, item_id)
    assert item.part_name == "Torsion spring 2in"
    assert item.unit_price == Decimal("42")
    assert item.unit_cost == Decimal("18.5")
    assert item.quantity == 12
    assert item.reorder_level == 3


def test_inventory_view_payload_keeps_unit_price(client_and_db):
    """The exact body InventoryView.submitForm sends on edit: no unit_price."""
    client, db = client_and_db
    item_id = _create(client)

    r = client.patch(f"/api/inventory/parts/{item_id}", json={
        "part_name": "Torsion spring", "name": "Torsion spring", "sku": "TS-207",
        "quantity": 12, "quantity_on_hand": 12, "reorder_level": 3,
        "reorder_point": 3, "unit_cost": 19.0, "supplier": None,
    })
    assert r.status_code == 200, r.text
    assert r.json()["unit_price"] == 42.0

    item = _item(db, item_id)
    assert item.unit_price == Decimal("42")
    assert item.unit_cost == Decimal("19")


def test_sent_fields_are_still_applied_including_zero(client_and_db):
    client, db = client_and_db
    item_id = _create(client)

    r = client.patch(f"/api/inventory/parts/{item_id}", json={"unit_price": 0, "reorder_level": 0})
    assert r.status_code == 200, r.text

    item = _item(db, item_id)
    assert item.unit_price == Decimal("0")
    assert item.reorder_level == 0
    assert item.quantity == 12


def test_create_audit_row_names_the_new_part(client_and_db):
    client, db = client_and_db
    item_id = _create(client)

    row = _audit(db, "create_part")
    assert row.entity_id == item_id
    assert row.tenant_id == TENANT
    assert row.user_id == USER
    assert row.details["part_name"] == "Torsion spring"
    assert row.details["sku"] == "TS-207"
    assert row.details["unit_price"] == 42.0
    assert row.details["quantity"] == 12


def test_update_audit_row_records_only_what_changed(client_and_db):
    client, db = client_and_db
    item_id = _create(client)

    r = client.patch(f"/api/inventory/parts/{item_id}", json={
        "part_name": "Torsion spring", "sku": "TS-207", "unit_cost": 20,
    })
    assert r.status_code == 200, r.text

    row = _audit(db, "update_part")
    assert row.entity_id == item_id
    assert row.tenant_id == TENANT
    assert row.user_id == USER
    assert row.details["changes"] == {"unit_cost": {"old": 18.5, "new": 20.0}}


def test_delete_and_adjust_audit_rows_carry_details(client_and_db):
    client, db = client_and_db
    item_id = _create(client)

    r = client.post(f"/api/inventory/items/{item_id}/adjust", json={"quantity_delta": -2, "reason": "used"})
    assert r.status_code == 200, r.text
    row = _audit(db, "adjust_stock")
    assert row.tenant_id == TENANT
    assert row.details["quantity_delta"] == -2
    assert row.details["reason"] == "used"
    assert row.details["new_quantity"] == 10

    r = client.delete(f"/api/inventory/parts/{item_id}")
    assert r.status_code == 204, r.text
    row = _audit(db, "delete_part")
    assert row.entity_id == item_id
    assert row.tenant_id == TENANT
    assert row.details == {"part_name": "Torsion spring", "sku": "TS-207"}
