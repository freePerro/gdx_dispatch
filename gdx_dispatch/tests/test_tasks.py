"""Tests for gdx_dispatch/routers/tasks.py — internal task/todo management."""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import tasks as tasks_router


def _make_client(tenant_id: str = "tenant-test"):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    _setup = SessionLocal()
    # replace inline DDL with metadata-based creation
    TenantBase.metadata.create_all(engine, checkfirst=True)
    _setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :cid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"g-{tenant_id}", "cid": tenant_id},
    )
    _setup.commit()
    _setup.close()

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(tasks_router.router)
    app.dependency_overrides[tasks_router.get_db] = _override_db
    app.dependency_overrides[tasks_router.get_current_user] = lambda: {
        "user_id": "test-user",
        "sub": "test-user",
        "email": "test@example.com",
        "role": "admin",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    return tc, SessionLocal, engine


@pytest.fixture()
def client():
    tc, SessionLocal, engine = _make_client()
    yield tc, SessionLocal
    tc.app.dependency_overrides.clear()
    engine.dispose()


def _payload(**overrides) -> dict:
    base = {
        "title": "Follow up with customer",
        "description": "Call about install schedule",
        "priority": "normal",
        "status": "open",
    }
    base.update(overrides)
    return base


def test_create_task(client):
    tc, _ = client
    r = tc.post("/api/tasks", json=_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert UUID(data["id"])
    assert data["title"] == "Follow up with customer"
    assert data["priority"] == "normal"
    assert data["status"] == "open"
    assert data["completed_at"] is None
    assert data["company_id"] == "tenant-test"


def test_list_tenant_scoped():
    tc_a, SessionA, engine_a = _make_client(tenant_id="tenant-a")
    tc_b, SessionB, engine_b = _make_client(tenant_id="tenant-b")

    ra = tc_a.post("/api/tasks", json=_payload(title="A-task"))
    assert ra.status_code == 201, ra.text
    rb = tc_b.post("/api/tasks", json=_payload(title="B-task"))
    assert rb.status_code == 201, rb.text

    list_a = tc_a.get("/api/tasks").json()
    list_b = tc_b.get("/api/tasks").json()
    assert [t["title"] for t in list_a] == ["A-task"]
    assert [t["title"] for t in list_b] == ["B-task"]

    tc_a.app.dependency_overrides.clear()
    tc_b.app.dependency_overrides.clear()
    engine_a.dispose()
    engine_b.dispose()


def test_filter_by_status(client):
    tc, _ = client
    t1 = tc.post("/api/tasks", json=_payload(title="open-1")).json()
    tc.post("/api/tasks", json=_payload(title="open-2"))
    done = tc.post("/api/tasks", json=_payload(title="done-1")).json()
    assert tc.post(f"/api/tasks/{done['id']}/complete").status_code == 200

    open_list = tc.get("/api/tasks", params={"status": "open"}).json()
    open_titles = {t["title"] for t in open_list}
    assert "open-1" in open_titles and "open-2" in open_titles
    assert "done-1" not in open_titles

    done_list = tc.get("/api/tasks", params={"status": "completed"}).json()
    done_titles = {t["title"] for t in done_list}
    assert done_titles == {"done-1"}
    assert t1["id"] in {t["id"] for t in open_list}


def test_complete_updates_timestamp(client):
    tc, _ = client
    created = tc.post("/api/tasks", json=_payload()).json()
    assert created["completed_at"] is None

    r = tc.post(f"/api/tasks/{created['id']}/complete")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


def test_reopen_clears_completed_at(client):
    tc, _ = client
    created = tc.post("/api/tasks", json=_payload()).json()
    tc.post(f"/api/tasks/{created['id']}/complete")

    r = tc.post(f"/api/tasks/{created['id']}/reopen")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "open"
    assert data["completed_at"] is None


def test_soft_delete(client):
    tc, SessionLocal = client
    created = tc.post("/api/tasks", json=_payload(title="delete-me")).json()

    r = tc.delete(f"/api/tasks/{created['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    assert tc.get(f"/api/tasks/{created['id']}").status_code == 404
    listed = tc.get("/api/tasks").json()
    assert all(t["id"] != created["id"] for t in listed)

    db = SessionLocal()
    try:
        row = db.execute(
            select(tasks_router.Task).where(tasks_router.Task.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
    finally:
        db.close()
