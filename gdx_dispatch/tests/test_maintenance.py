"""Tests for the maintenance plans / enrollments router."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.maintenance import PlanEnrollment, router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tenant_module_grants (
                id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT
            )
            """
        )
    )
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._SessionLocal = Session  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _plan_payload(**overrides) -> dict:
    base = {
        "name": "Annual Garage Door Tune-Up",
        "description": "Yearly inspection, lube, safety check",
        "visits_per_year": 1,
        "billing_type": "annual",
        "price": 199.00,
        "active": True,
    }
    base.update(overrides)
    return base


def _enrollment_payload(plan_id: str, **overrides) -> dict:
    base = {
        "plan_id": plan_id,
        "customer_id": str(uuid4()),
        "notes": "Preferred morning appointments",
    }
    base.update(overrides)
    return base


def test_create_plan(client: TestClient):
    r = client.post("/api/maintenance/plans", json=_plan_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["name"] == "Annual Garage Door Tune-Up"
    assert data["visits_per_year"] == 1
    assert data["billing_type"] == "annual"
    assert data["price"] == 199.0
    assert data["active"] is True
    assert data["company_id"] == "tenant-test"


def test_create_enrollment_sets_next_service_date(client: TestClient):
    # visits_per_year=2 → 6 months between visits
    plan = client.post(
        "/api/maintenance/plans",
        json=_plan_payload(name="Bi-Annual", visits_per_year=2),
    ).json()

    start = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    r = client.post(
        "/api/maintenance/enrollments",
        json=_enrollment_payload(plan["id"], start_date=start.isoformat()),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "active"
    assert data["visits_completed"] == 0
    assert data["next_service_date"] is not None
    nsd = datetime.fromisoformat(data["next_service_date"])
    # 6 months after Jan 15 = July 15
    assert nsd.year == 2026
    assert nsd.month == 7
    assert nsd.day == 15


def test_list_enrollments_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        p1 = c1.post("/api/maintenance/plans", json=_plan_payload(name="A-plan")).json()
        p2 = c2.post("/api/maintenance/plans", json=_plan_payload(name="B-plan")).json()
        c1.post(
            "/api/maintenance/enrollments", json=_enrollment_payload(p1["id"])
        )
        c2.post(
            "/api/maintenance/enrollments", json=_enrollment_payload(p2["id"])
        )

        l1 = c1.get("/api/maintenance/enrollments").json()
        l2 = c2.get("/api/maintenance/enrollments").json()
        assert len(l1) == 1
        assert len(l2) == 1
        assert l1[0]["plan_id"] == p1["id"]
        assert l2[0]["plan_id"] == p2["id"]

        # Plans are also tenant scoped
        assert len(c1.get("/api/maintenance/plans").json()) == 1
        assert len(c2.get("/api/maintenance/plans").json()) == 1
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_advance_increments_and_reschedules(client: TestClient):
    plan = client.post(
        "/api/maintenance/plans",
        json=_plan_payload(visits_per_year=4),  # quarterly → 3 months
    ).json()
    start = datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
    enrollment = client.post(
        "/api/maintenance/enrollments",
        json=_enrollment_payload(plan["id"], start_date=start.isoformat()),
    ).json()
    first_next = datetime.fromisoformat(enrollment["next_service_date"])
    assert first_next.month == 4  # Jan + 3 months

    r = client.post(f"/api/maintenance/enrollments/{enrollment['id']}/advance")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["visits_completed"] == 1
    second_next = datetime.fromisoformat(data["next_service_date"])
    assert second_next.month == 7  # April + 3 months


def test_due_this_month_filter(client: TestClient):
    plan = client.post(
        "/api/maintenance/plans", json=_plan_payload(visits_per_year=1)
    ).json()
    enrollment = client.post(
        "/api/maintenance/enrollments", json=_enrollment_payload(plan["id"])
    ).json()

    # Manually override next_service_date via DB to be inside the current month
    SessionLocal = client._SessionLocal  # type: ignore[attr-defined]
    db = SessionLocal()
    try:
        from uuid import UUID as _UUID

        row = db.get(PlanEnrollment, _UUID(enrollment["id"]))
        now = datetime.now(timezone.utc)
        row.next_service_date = now.replace(day=15, hour=9, minute=0, second=0, microsecond=0)
        db.commit()
    finally:
        db.close()

    r = client.get("/api/maintenance/due-this-month")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(e["id"] == enrollment["id"] for e in rows)

    # And one that is NOT in current month should not show
    db = SessionLocal()
    try:
        from uuid import UUID as _UUID

        row = db.get(PlanEnrollment, _UUID(enrollment["id"]))
        # Push far into the future
        row.next_service_date = datetime(2099, 1, 15, tzinfo=timezone.utc)
        db.commit()
    finally:
        db.close()

    r2 = client.get("/api/maintenance/due-this-month")
    assert all(e["id"] != enrollment["id"] for e in r2.json())


def test_pause_enrollment(client: TestClient):
    plan = client.post("/api/maintenance/plans", json=_plan_payload()).json()
    e = client.post(
        "/api/maintenance/enrollments", json=_enrollment_payload(plan["id"])
    ).json()
    r = client.patch(
        f"/api/maintenance/enrollments/{e['id']}", json={"status": "paused"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"

    # Cannot advance a paused enrollment
    adv = client.post(f"/api/maintenance/enrollments/{e['id']}/advance")
    assert adv.status_code == 400


def test_billing_type_validation(client: TestClient):
    r = client.post(
        "/api/maintenance/plans", json=_plan_payload(billing_type="weekly")
    )
    assert r.status_code == 422
