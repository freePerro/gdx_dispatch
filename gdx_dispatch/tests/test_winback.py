"""Tests for the winback router (campaigns + follow-ups)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.winback import FollowUp, WinbackSend, router


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
            VALUES (:id, :tid, 'customers', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'customers', datetime('now'), datetime('now'))
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


def _campaign_payload(**overrides) -> dict:
    base = {
        "name": "Spring Winback 2026",
        "channel": "sms",
        "subject": None,
        "body_template": "Hi {customer_name}, we miss you! Book now for 10% off.",
        "inactivity_months": 6,
    }
    base.update(overrides)
    return base


def _follow_up_payload(**overrides) -> dict:
    base = {
        "entity_type": "estimate",
        "entity_id": "est-123",
        "assigned_to": "rep@example.com",
        "due_date": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "note": "Call about the sent estimate",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Campaign tests
# ---------------------------------------------------------------------------


def test_create_campaign(client: TestClient):
    r = client.post("/api/winback/campaigns", json=_campaign_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["name"] == "Spring Winback 2026"
    assert data["status"] == "draft"
    assert data["channel"] == "sms"
    assert data["inactivity_months"] == 6
    assert data["company_id"] == "tenant-test"
    assert data["sent_at"] is None


def test_list_campaigns_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        c1.post("/api/winback/campaigns", json=_campaign_payload(name="A only"))
        c2.post("/api/winback/campaigns", json=_campaign_payload(name="B only"))

        list_a = c1.get("/api/winback/campaigns").json()
        list_b = c2.get("/api/winback/campaigns").json()
        assert len(list_a) == 1
        assert len(list_b) == 1
        assert list_a[0]["name"] == "A only"
        assert list_b[0]["name"] == "B only"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_send_campaign_enqueues_sends(client: TestClient):
    # Create a campaign
    created = client.post("/api/winback/campaigns", json=_campaign_payload()).json()

    # Provide explicit override_customer_ids to bypass candidates query
    cust_ids = [str(uuid4()) for _ in range(3)]
    r = client.post(
        f"/api/winback/campaigns/{created['id']}/send",
        json={"override_customer_ids": cust_ids},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["campaign_id"] == created["id"]
    assert body["enqueued"] == 3

    # Verify WinbackSend rows exist in DB
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        sends = db.execute(
            select(WinbackSend).where(WinbackSend.campaign_id == UUID(created["id"]))
        ).scalars().all()
        assert len(sends) == 3
        for s in sends:
            assert s.status == "queued"
            assert s.channel == "sms"
            assert s.company_id == "tenant-test"
    finally:
        db.close()

    # Campaign status now "sent"
    listed = client.get("/api/winback/campaigns").json()
    assert listed[0]["status"] == "sent"
    assert listed[0]["sent_at"] is not None


def test_channel_validation(client: TestClient):
    r = client.post("/api/winback/campaigns", json=_campaign_payload(channel="carrier-pigeon"))
    assert r.status_code == 422


def test_candidates_endpoint_returns_empty_when_no_tables(client: TestClient):
    # Test DB doesn't have a customers/jobs table → should catch and return []
    r = client.get("/api/winback/candidates", params={"months": 6})
    assert r.status_code == 200
    assert r.json() == []


def test_stats_endpoint(client: TestClient):
    client.post("/api/winback/campaigns", json=_campaign_payload(name="One"))
    client.post("/api/winback/campaigns", json=_campaign_payload(name="Two"))
    r = client.get("/api/winback/stats")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_campaigns"] == 2
    assert data["active"] == 2
    assert data["sent_last_30d"] == 0
    assert data["candidates_count"] == 0


# ---------------------------------------------------------------------------
# Follow-up tests
# ---------------------------------------------------------------------------


def test_create_follow_up(client: TestClient):
    r = client.post("/api/follow-ups", json=_follow_up_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["entity_type"] == "estimate"
    assert data["entity_id"] == "est-123"
    assert data["status"] == "open"
    assert data["company_id"] == "tenant-test"


def test_list_follow_ups_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        c1.post("/api/follow-ups", json=_follow_up_payload(note="A task"))
        c2.post("/api/follow-ups", json=_follow_up_payload(note="B task"))

        la = c1.get("/api/follow-ups").json()
        lb = c2.get("/api/follow-ups").json()
        assert len(la) == 1 and la[0]["note"] == "A task"
        assert len(lb) == 1 and lb[0]["note"] == "B task"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_list_follow_ups_filters(client: TestClient):
    a = client.post("/api/follow-ups", json=_follow_up_payload(entity_type="estimate")).json()
    b = client.post("/api/follow-ups", json=_follow_up_payload(entity_type="invoice", entity_id="inv-1")).json()

    only_estimates = client.get("/api/follow-ups", params={"entity_type": "estimate"}).json()
    assert len(only_estimates) == 1
    assert only_estimates[0]["id"] == a["id"]

    only_invoices = client.get("/api/follow-ups", params={"entity_type": "invoice"}).json()
    assert len(only_invoices) == 1
    assert only_invoices[0]["id"] == b["id"]


def test_complete_follow_up(client: TestClient):
    created = client.post("/api/follow-ups", json=_follow_up_payload()).json()
    r = client.post(f"/api/follow-ups/{created['id']}/complete")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None

    # Completed tasks no longer in default list (which filters status=open)
    listed = client.get("/api/follow-ups").json()
    assert all(f["id"] != created["id"] for f in listed)

    # But appear when filtered by status
    completed_list = client.get("/api/follow-ups", params={"status": "completed"}).json()
    assert any(f["id"] == created["id"] for f in completed_list)


def test_bulk_complete(client: TestClient):
    ids = []
    for i in range(3):
        r = client.post("/api/follow-ups", json=_follow_up_payload(entity_id=f"e-{i}"))
        ids.append(r.json()["id"])

    r = client.post("/api/follow-ups/bulk-send", json={"ids": ids})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["completed"] == 3
    assert data["requested"] == 3

    open_list = client.get("/api/follow-ups").json()
    assert len(open_list) == 0


def test_soft_delete(client: TestClient):
    created = client.post("/api/follow-ups", json=_follow_up_payload()).json()
    r = client.delete(f"/api/follow-ups/{created['id']}")
    assert r.status_code == 204

    # Not in list
    listed = client.get("/api/follow-ups").json()
    assert all(f["id"] != created["id"] for f in listed)

    # Row still exists with deleted_at set
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(
            select(FollowUp).where(FollowUp.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
        assert row.status == "cancelled"
    finally:
        db.close()


def test_follow_up_entity_type_validation(client: TestClient):
    r = client.post(
        "/api/follow-ups",
        json=_follow_up_payload(entity_type="not-a-real-type"),
    )
    assert r.status_code == 422
