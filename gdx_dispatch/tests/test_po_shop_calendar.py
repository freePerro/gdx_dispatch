"""A purchase order's dates are the shop's calendar day, not the container's UTC one.

The containers run on UTC, so ``date.today()`` is tomorrow for the last five or
six hours of a Minnesota evening. A PO written or received at 10pm Central was
dated the next day (GDXA-507). Every test here freezes the clock between UTC
midnight and shop midnight, where the two calendars disagree, so each fails on
the old ``date.today()`` / ``datetime.now()`` code.
"""
from __future__ import annotations

from datetime import date

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.purchase_orders.service import create_po

# 03:00 UTC on Oct 11 is 22:00 on Oct 10 in Chicago.
LATE_EVENING = "2026-10-11T03:00:00+00:00"
SHOP_DAY = date(2026, 10, 10)
UTC_DAY = date(2026, 10, 11)


@pytest.fixture()
def po_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.models.tenant_models import AppSettings
    from gdx_dispatch.routers.auth import get_current_user
    from gdx_dispatch.routers.purchase_orders import router as po_router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text("CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT, UNIQUE(company_id, module_key))"))
    setup.execute(text("INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) VALUES ('g1','tenant-test','inventory',datetime('now'),datetime('now'))"))
    setup.add(AppSettings(timezone="America/Chicago"))
    setup.commit()
    setup.close()

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(po_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1", "role": "admin", "tenant_id": "tenant-test"}
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()
    engine.dispose()


def _create(po_client, **extra):
    r = po_client.post("/api/purchase-orders", json={
        "vendor_name": "Door Supply", "lines": [{"description": "Spring", "quantity_ordered": 1, "unit_cost": 40}], **extra,
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_a_po_written_late_evening_is_ordered_on_the_shop_day(po_client):
    with freeze_time(LATE_EVENING):
        po = _create(po_client)
    assert po["order_date"] == SHOP_DAY.isoformat(), f"dated {po['order_date']}; the UTC day is {UTC_DAY}"


def test_an_explicit_order_date_is_kept(po_client):
    with freeze_time(LATE_EVENING):
        po = _create(po_client, order_date="2026-10-01")
    assert po["order_date"] == "2026-10-01"


def test_a_po_received_late_evening_is_received_on_the_shop_day(po_client):
    po = _create(po_client, order_date="2026-10-01")
    with freeze_time(LATE_EVENING):
        r = po_client.post(f"/api/purchase-orders/{po['id']}/receive")
    assert r.status_code == 200, r.text
    assert r.json()["received_date"] == SHOP_DAY.isoformat()


def test_the_order_date_column_has_no_utc_default():
    """A writer that omits order_date must fail, not silently take the UTC day."""
    from gdx_dispatch.routers.purchase_orders import PurchaseOrder

    assert PurchaseOrder.__table__.c.order_date.default is None


def test_module_service_po_number_carries_the_shop_day(tenant_db):
    from gdx_dispatch.models.tenant_models import AppSettings

    tenant_db.add(AppSettings(timezone="America/Chicago"))
    tenant_db.commit()
    with freeze_time(LATE_EVENING):
        po = create_po("Door Supply", None, [{"description": "Spring", "qty": 1, "unit_cost": 40}], tenant_db)
    assert po.po_number.startswith(f"PO-{SHOP_DAY:%Y%m%d}-"), po.po_number
