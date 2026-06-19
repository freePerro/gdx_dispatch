"""Tests for the dispatch appointments router."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.appointments import Appointment, router
from gdx_dispatch.routers.auth import get_current_user


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
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _payload(**overrides) -> dict:
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    end = start + timedelta(hours=1)
    base = {
        "title": "Spring replacement",
        "description": "Broken torsion spring",
        "tech_id": "tech-42",
        "address": "123 Main St",
        "lat": 39.7392,
        "lng": -104.9903,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
    }
    base.update(overrides)
    return base


def test_create_appointment(client: TestClient):
    r = client.post("/api/appointments", json=_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["title"] == "Spring replacement"
    assert data["status"] == "scheduled"
    assert data["tech_id"] == "tech-42"
    assert data["company_id"] == "tenant-test"
    assert data["confirmed_at"] is None
    assert data["en_route_at"] is None


def test_reject_end_before_start(client: TestClient):
    start = datetime.now(timezone.utc) + timedelta(hours=5)
    end = start - timedelta(hours=1)
    r = client.post(
        "/api/appointments",
        json=_payload(start_at=start.isoformat(), end_at=end.isoformat()),
    )
    assert r.status_code == 422


def test_list_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post("/api/appointments", json=_payload(title="A-only"))
        assert r1.status_code == 201
        r2 = c2.post("/api/appointments", json=_payload(title="B-only"))
        assert r2.status_code == 201

        # Use a wide window so we catch today+tomorrow regardless of UTC boundary.
        today = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (
            datetime.now(timezone.utc).date() + timedelta(days=2)
        ).isoformat()
        params = {"start": today, "end": tomorrow}
        list1 = c1.get("/api/appointments", params=params).json()
        list2 = c2.get("/api/appointments", params=params).json()
        assert len(list1) == 1
        assert len(list2) == 1
        assert list1[0]["title"] == "A-only"
        assert list2[0]["title"] == "B-only"

        cross = c1.get(f"/api/appointments/{list2[0]['id']}")
        assert cross.status_code == 404
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_list_by_tech_id_filter(client: TestClient):
    client.post("/api/appointments", json=_payload(tech_id="tech-A", title="A1"))
    client.post("/api/appointments", json=_payload(tech_id="tech-B", title="B1"))
    client.post("/api/appointments", json=_payload(tech_id="tech-A", title="A2"))
    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (
        datetime.now(timezone.utc).date() + timedelta(days=2)
    ).isoformat()
    r = client.get(
        "/api/appointments",
        params={"tech_id": "tech-A", "start": today, "end": tomorrow},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert all(a["tech_id"] == "tech-A" for a in data)


def test_confirm_sets_timestamp(client: TestClient):
    created = client.post("/api/appointments", json=_payload()).json()
    r = client.post(f"/api/appointments/{created['id']}/confirm")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "confirmed"
    assert data["confirmed_at"] is not None


def test_on_my_way_sets_timestamp(client: TestClient):
    created = client.post("/api/appointments", json=_payload()).json()
    r = client.post(f"/api/appointments/{created['id']}/on-my-way")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "en_route"
    assert data["en_route_at"] is not None


def test_status_transition_flow(client: TestClient):
    created = client.post("/api/appointments", json=_payload()).json()
    aid = created["id"]
    assert created["status"] == "scheduled"

    r = client.post(f"/api/appointments/{aid}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "confirmed"

    r = client.post(f"/api/appointments/{aid}/on-my-way")
    assert r.status_code == 200 and r.json()["status"] == "en_route"

    r = client.post(f"/api/appointments/{aid}/arrived")
    assert r.status_code == 200 and r.json()["status"] == "arrived"
    assert r.json()["arrived_at"] is not None

    r = client.post(f"/api/appointments/{aid}/complete")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert r.json()["completed_at"] is not None


def test_cancel_with_reason(client: TestClient):
    created = client.post("/api/appointments", json=_payload()).json()
    r = client.post(
        f"/api/appointments/{created['id']}/cancel",
        json={"reason": "Customer rescheduled"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "cancelled"
    assert data["notes"] is not None
    assert "cancel reason: Customer rescheduled" in data["notes"]


def test_soft_delete(client: TestClient):
    created = client.post("/api/appointments", json=_payload()).json()
    r = client.delete(f"/api/appointments/{created['id']}")
    assert r.status_code == 204

    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (
        datetime.now(timezone.utc).date() + timedelta(days=2)
    ).isoformat()
    listed = client.get(
        "/api/appointments", params={"start": today, "end": tomorrow}
    ).json()
    assert all(a["id"] != created["id"] for a in listed)

    gone = client.get(f"/api/appointments/{created['id']}")
    assert gone.status_code == 404

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(
            select(Appointment).where(Appointment.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
    finally:
        db.close()


def test_unconfirmed_filter(client: TestClient):
    # Both within next 48h; one confirmed, one still scheduled.
    a1 = client.post("/api/appointments", json=_payload(title="Needs confirm")).json()
    a2 = client.post("/api/appointments", json=_payload(title="Already confirmed")).json()
    client.post(f"/api/appointments/{a2['id']}/confirm")

    r = client.get("/api/appointments/unconfirmed", params={"hours": 48})
    assert r.status_code == 200, r.text
    data = r.json()
    ids = {a["id"] for a in data}
    assert a1["id"] in ids
    assert a2["id"] not in ids
    assert all(a["status"] == "scheduled" for a in data)
