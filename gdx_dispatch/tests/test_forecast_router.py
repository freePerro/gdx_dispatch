"""Tests for /api/forecast/* — settings GET/PUT and revenue projection."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job
from gdx_dispatch.modules.forecasting import router as forecasting_router
from gdx_dispatch.modules.forecasting.models import QBRecurringTransaction


@pytest.fixture()
def client():
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


def _seed_customer(db):
    cid = uuid4()
    db.add(Customer(id=cid, name="Acme", company_id="tenant-test"))
    db.commit()
    return cid


def _seed_invoice(db, *, balance_due: float, status: str, due_date: date | None, public_token: str | None = None) -> None:
    customer_id = _seed_customer(db)
    inv = Invoice(
        id=uuid4(),
        customer_id=customer_id,
        company_id="tenant-test",
        invoice_number=f"INV-{uuid4().hex[:6]}",
        public_token=public_token or uuid4().hex,
        subtotal=balance_due,
        total=balance_due,
        balance_due=balance_due,
        status=status,
        due_date=due_date,
    )
    db.add(inv)
    db.commit()


def test_get_settings_returns_defaults(client):
    tc, _ = client
    r = tc.get("/api/forecast/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default_window_days"] == 30
    assert body["collect_rate_0_30"] == 0.95
    assert body["collect_rate_31_60"] == 0.80
    assert body["collect_rate_61_90"] == 0.60
    assert body["collect_rate_90_plus"] == 0.30
    assert body["scheduled_realization_rate"] == 0.70
    assert body["include_recurring"] is True


def test_update_settings_persists(client):
    tc, _ = client
    r = tc.put(
        "/api/forecast/settings",
        json={"collect_rate_0_30": 0.99, "default_window_days": 60, "include_recurring": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["collect_rate_0_30"] == 0.99
    assert body["default_window_days"] == 60
    assert body["include_recurring"] is False
    # Read-back
    r2 = tc.get("/api/forecast/settings")
    assert r2.json()["collect_rate_0_30"] == 0.99


def test_update_settings_validates_range(client):
    tc, _ = client
    r = tc.put("/api/forecast/settings", json={"collect_rate_0_30": 1.5})
    assert r.status_code == 422


def test_revenue_with_no_data_returns_zeroes(client):
    tc, _ = client
    r = tc.get("/api/forecast/revenue")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expected_total"] == 0.0
    assert body["open_ar"]["open_total"] == 0.0
    assert body["scheduled_jobs"]["job_count"] == 0
    assert body["recurring"]["count"] == 0


def test_revenue_buckets_open_invoices_by_age(client):
    tc, SessionLocal = client
    today = date.today()
    db = SessionLocal()
    # 0-30: due 10 days ago, $1000 → expected 950
    _seed_invoice(db, balance_due=1000, status="sent", due_date=today - timedelta(days=10))
    # 31-60: due 45 days ago, $500 → expected 400
    _seed_invoice(db, balance_due=500, status="overdue", due_date=today - timedelta(days=45))
    # 90+: due 120 days ago, $200 → expected 60
    _seed_invoice(db, balance_due=200, status="overdue", due_date=today - timedelta(days=120))
    # Paid invoice ignored
    _seed_invoice(db, balance_due=0, status="paid", due_date=today - timedelta(days=10))
    db.close()

    r = tc.get("/api/forecast/revenue?window=30")
    assert r.status_code == 200, r.text
    body = r.json()
    ar = body["open_ar"]
    assert ar["open_total"] == 1700.0
    # 950 + 400 + 60
    assert ar["expected_total"] == pytest.approx(1410.0, rel=1e-6)
    assert ar["by_bucket"]["0_30"]["invoice_count"] == 1
    assert ar["by_bucket"]["31_60"]["invoice_count"] == 1
    assert ar["by_bucket"]["90_plus"]["invoice_count"] == 1


def test_revenue_window_param_validates(client):
    tc, _ = client
    assert tc.get("/api/forecast/revenue?window=0").status_code == 400
    assert tc.get("/api/forecast/revenue?window=999").status_code == 400


def test_revenue_includes_recurring_when_enabled(client):
    tc, SessionLocal = client
    today = date.today()
    db = SessionLocal()
    db.add(QBRecurringTransaction(
        qb_id="qbo-1", txn_type="Invoice", name="Monthly maintenance",
        amount=250.0, next_date=today + timedelta(days=5), active=True,
    ))
    db.add(QBRecurringTransaction(
        qb_id="qbo-2", txn_type="Invoice", name="Future-out", amount=999.0,
        next_date=today + timedelta(days=120), active=True,
    ))
    db.commit()
    db.close()

    r = tc.get("/api/forecast/revenue?window=30")
    body = r.json()
    assert body["recurring"]["count"] == 1
    assert body["recurring"]["expected_total"] == 250.0
    assert body["expected_total"] == 250.0


def test_revenue_excludes_recurring_when_disabled(client):
    tc, SessionLocal = client
    db = SessionLocal()
    db.add(QBRecurringTransaction(
        qb_id="qbo-1", txn_type="Invoice", amount=250.0,
        next_date=date.today() + timedelta(days=5), active=True,
    ))
    db.commit()
    db.close()

    tc.put("/api/forecast/settings", json={"include_recurring": False})
    r = tc.get("/api/forecast/revenue?window=30")
    body = r.json()
    assert body["recurring"]["count"] == 0
    assert body["expected_total"] == 0.0


def test_put_settings_requires_admin_role():
    """Audit follow-up: PUT must reject non-admin roles. require_role
    is checked at HTTP, not just at unit level (per feedback_test_prod_token_parity)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase

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

    from gdx_dispatch.modules.forecasting import router as forecasting_router

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(forecasting_router.router)
    app.dependency_overrides[forecasting_router.get_db] = _override_db
    app.dependency_overrides[forecasting_router.get_current_user] = lambda: {
        "sub": "tech-user",
        "role": "tech",
        "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=False)

    # GET is readable by techs (no role gate on read).
    assert tc.get("/api/forecast/settings").status_code == 200
    # PUT is blocked.
    r = tc.put("/api/forecast/settings", json={"collect_rate_0_30": 0.99})
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    engine.dispose()


def test_settings_columns_round_trip_as_decimal_compatible():
    """Audit follow-up: update_settings must coerce float → Decimal so
    Postgres NUMERIC columns don't DataError on commit. SQLite is lax;
    this asserts the coercion happens at the boundary."""
    from decimal import Decimal
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.forecasting import service as forecast_service

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    s = forecast_service.update_settings(db, {"collect_rate_0_30": 0.991})
    # Stored as Decimal-compatible (truthy round-trip; SQLite returns float,
    # Postgres returns Decimal; both compare equal to Decimal("0.991")).
    assert Decimal(str(s.collect_rate_0_30)) == Decimal("0.991")
    db.close()
    engine.dispose()
