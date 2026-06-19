"""Tests for QB recurring sync helper and endpoints.

The fetch call to QBO is monkeypatched — we don't make real network calls.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.forecasting import qb_recurring as qb_recurring_helper
from gdx_dispatch.modules.forecasting import router as forecasting_router
from gdx_dispatch.modules.forecasting.models import QBRecurringTransaction


SAMPLE_QBO_RESPONSE = [
    {
        "Invoice": {
            "Id": "1001",
            "DocNumber": "INV-R-1",
            "TotalAmt": 250.00,
            "CustomerRef": {"value": "42", "name": "Acme Co"},
            "RecurringInfo": {
                "Name": "Monthly maintenance",
                "Active": True,
                "ScheduleInfo": {
                    "NextDate": "2026-06-01",
                    "IntervalType": "Monthly",
                    "NumInterval": 1,
                },
            },
        }
    },
    {
        "Bill": {
            "Id": "2002",
            "TotalAmt": 80.00,
            "VendorRef": {"value": "9", "name": "Utilities Inc"},
            "RecurringInfo": {
                "Active": False,
                "ScheduleInfo": {
                    "NextDate": "2026-06-15",
                    "IntervalType": "Monthly",
                },
            },
        }
    },
]


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # Bypass QBO auth + network: route sync_recurring_for_tenant straight
    # into upsert_recurring with the canned response.
    def _fake_sync(tenant_id: str, db):
        return qb_recurring_helper.upsert_recurring(db, SAMPLE_QBO_RESPONSE)

    monkeypatch.setattr(qb_recurring_helper, "sync_recurring_for_tenant", _fake_sync)
    monkeypatch.setattr(forecasting_router.qb_recurring_helper, "sync_recurring_for_tenant", _fake_sync)

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(forecasting_router.router)
    app.dependency_overrides[forecasting_router.get_db] = _override_db
    app.dependency_overrides[forecasting_router.get_current_user] = lambda: {
        "sub": "test-user",
        "role": "admin",
        "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def test_upsert_recurring_creates_rows_with_correct_shape():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    result = qb_recurring_helper.upsert_recurring(db, SAMPLE_QBO_RESPONSE)
    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["total"] == 2

    rows = db.execute(select(QBRecurringTransaction).order_by(QBRecurringTransaction.qb_id)).scalars().all()
    assert len(rows) == 2

    inv = next(r for r in rows if r.qb_id == "1001")
    assert inv.txn_type == "Invoice"
    assert inv.name == "Monthly maintenance"
    assert inv.customer_qb_id == "42"
    assert inv.customer_name == "Acme Co"
    assert float(inv.amount) == 250.00
    assert inv.next_date == date(2026, 6, 1)
    assert inv.interval_type == "Monthly"
    assert int(inv.num_interval) == 1
    assert inv.active is True

    bill = next(r for r in rows if r.qb_id == "2002")
    assert bill.txn_type == "Bill"
    assert bill.active is False
    db.close()
    engine.dispose()


def test_upsert_idempotent_updates_existing():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    qb_recurring_helper.upsert_recurring(db, SAMPLE_QBO_RESPONSE)
    # Same payload twice → no duplicates, all updates the second time
    result = qb_recurring_helper.upsert_recurring(db, SAMPLE_QBO_RESPONSE)
    assert result["created"] == 0
    assert result["updated"] == 2

    count = db.execute(select(QBRecurringTransaction)).scalars().all()
    assert len(count) == 2
    db.close()
    engine.dispose()


def test_sync_endpoint_invokes_upsert(client):
    tc, SessionLocal = client
    r = tc.post("/api/quickbooks/sync/recurring-transactions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2


def test_list_endpoint_returns_synced_rows(client):
    tc, SessionLocal = client
    tc.post("/api/quickbooks/sync/recurring-transactions")
    r = tc.get("/api/quickbooks/recurring-transactions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    qb_ids = sorted(item["qb_id"] for item in body["items"])
    assert qb_ids == ["1001", "2002"]


def test_sync_endpoint_surfaces_qbo_error_as_502(monkeypatch):
    """Audit follow-up: a QBO 503 / 500 / rate-limit must NOT show as
    `Recurring: 0` to the user. The sync helper raises QBError; the
    router returns 502 with the message."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.quickbooks import QBError

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    def _raise(tenant_id, db):
        raise QBError("QuickBooks recurring fetch failed (HTTP 503)")

    monkeypatch.setattr(forecasting_router.qb_recurring_helper, "sync_recurring_for_tenant", _raise)

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(forecasting_router.router)
    app.dependency_overrides[forecasting_router.get_db] = _override_db
    app.dependency_overrides[forecasting_router.get_current_user] = lambda: {
        "sub": "test-user", "role": "admin", "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post("/api/quickbooks/sync/recurring-transactions")
    assert r.status_code == 502
    assert "503" in r.json()["detail"]
    engine.dispose()
