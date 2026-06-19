"""Tests for the Good/Better/Best proposals router."""
from __future__ import annotations

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
from gdx_dispatch.routers.proposals import Proposal, router


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
            VALUES (:id, :tid, 'estimates', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'estimates', datetime('now'), datetime('now'))
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
    base = {
        "title": "Garage Door Replacement",
        "description": "Full install with haul-away",
        "good_price": 1200.00,
        "better_price": 1800.00,
        "best_price": 2500.00,
        "good_description": "16x7 steel, no insulation",
        "better_description": "16x7 insulated, two windows",
        "best_description": "16x7 full insulated, smart opener, warranty",
        "customer_name": "Jane Homeowner",
    }
    base.update(overrides)
    return base


def test_create_proposal(client: TestClient):
    r = client.post("/api/proposals", json=_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["title"] == "Garage Door Replacement"
    assert data["status"] == "draft"
    assert data["good_price"] == 1200.0
    assert data["better_price"] == 1800.0
    assert data["best_price"] == 2500.0
    assert data["chosen_tier"] is None
    assert data["sent_at"] is None
    assert data["company_id"] == "tenant-test"


def test_list_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post("/api/proposals", json=_payload(title="A-only"))
        assert r1.status_code == 201
        r2 = c2.post("/api/proposals", json=_payload(title="B-only"))
        assert r2.status_code == 201

        list1 = c1.get("/api/proposals").json()
        list2 = c2.get("/api/proposals").json()
        assert len(list1) == 1
        assert len(list2) == 1
        assert list1[0]["title"] == "A-only"
        assert list2[0]["title"] == "B-only"

        # tenant A cannot fetch tenant B's proposal by ID
        cross = c1.get(f"/api/proposals/{list2[0]['id']}")
        assert cross.status_code == 404
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_send_updates_status_and_timestamp(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    r = client.post(f"/api/proposals/{created['id']}/send")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "sent"
    assert data["sent_at"] is not None


def test_accept_requires_valid_tier(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    r = client.post(
        f"/api/proposals/{created['id']}/accept",
        json={"tier": "platinum"},
    )
    assert r.status_code == 422


def test_accept_sets_chosen_tier(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    client.post(f"/api/proposals/{created['id']}/send")
    r = client.post(
        f"/api/proposals/{created['id']}/accept",
        json={"tier": "better"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert data["chosen_tier"] == "better"
    assert data["accepted_at"] is not None


def test_cannot_edit_after_sent(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    send_r = client.post(f"/api/proposals/{created['id']}/send")
    assert send_r.status_code == 200
    r = client.patch(
        f"/api/proposals/{created['id']}",
        json={"title": "New title attempt"},
    )
    assert r.status_code == 400
    assert "draft" in r.json()["detail"].lower()


def test_soft_delete(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    r = client.delete(f"/api/proposals/{created['id']}")
    assert r.status_code == 204

    # Excluded from list
    listed = client.get("/api/proposals").json()
    assert all(p["id"] != created["id"] for p in listed)

    # GET returns 404
    gone = client.get(f"/api/proposals/{created['id']}")
    assert gone.status_code == 404

    # Row still exists with deleted_at set
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(
            select(Proposal).where(Proposal.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
    finally:
        db.close()


def test_pydantic_bounds_reject_oversized_title(client: TestClient):
    huge_title = "x" * 301
    r = client.post("/api/proposals", json=_payload(title=huge_title))
    assert r.status_code == 422


def test_decline_proposal_sets_status(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    r = client.post(f"/api/proposals/{created['id']}/decline")
    assert r.status_code == 200
    assert r.json()["status"] == "declined"


def test_list_filters_by_status(client: TestClient):
    p1 = client.post("/api/proposals", json=_payload(title="Draft one")).json()
    p2 = client.post("/api/proposals", json=_payload(title="Sent one")).json()
    client.post(f"/api/proposals/{p2['id']}/send")

    drafts = client.get("/api/proposals", params={"status": "draft"}).json()
    sents = client.get("/api/proposals", params={"status": "sent"}).json()
    assert len(drafts) == 1 and drafts[0]["id"] == p1["id"]
    assert len(sents) == 1 and sents[0]["id"] == p2["id"]


def test_patch_updates_draft_fields(client: TestClient):
    created = client.post("/api/proposals", json=_payload()).json()
    r = client.patch(
        f"/api/proposals/{created['id']}",
        json={"title": "Updated title", "good_price": 1500.0},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Updated title"
    assert data["good_price"] == 1500.0


def test_get_404_for_missing(client: TestClient):
    r = client.get(f"/api/proposals/{uuid4()}")
    assert r.status_code == 404
