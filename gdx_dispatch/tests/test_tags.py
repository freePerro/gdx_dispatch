"""Tests for the per-tenant tags router."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.tags import router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    # Module-grant tables are created on-demand by gdx_dispatch.core.modules at runtime.
    # Tests must create them explicitly so the grant inserts below succeed.
    setup.execute(text(
        """CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            module_key TEXT NOT NULL,
            granted_at TIMESTAMP,
            created_at TIMESTAMP
        )"""
    ))
    setup.execute(text(
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            module_key TEXT NOT NULL,
            granted_at TIMESTAMP,
            created_at TIMESTAMP
        )"""
    ))
    for mod_key in ("jobs", "customers", "core"):
        setup.execute(
            text(
                """
                INSERT OR IGNORE INTO tenant_module_grants
                    (id, tenant_id, module_key, granted_at, created_at)
                VALUES (:id, :tid, :mk, datetime('now'), datetime('now'))
                """
            ),
            {"id": f"g1-{tenant_id}-{mod_key}", "tid": tenant_id, "mk": mod_key},
        )
        setup.execute(
            text(
                """
                INSERT OR IGNORE INTO company_module_grants
                    (id, company_id, module_key, granted_at, created_at)
                VALUES (:id, :tid, :mk, datetime('now'), datetime('now'))
                """
            ),
            {"id": f"g2-{tenant_id}-{mod_key}", "tid": tenant_id, "mk": mod_key},
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


def test_create_tag(client: TestClient):
    r = client.post("/api/tags", json={"name": "VIP", "color": "#ff0000", "description": "Priority"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["name"] == "VIP"
    assert data["color"] == "#ff0000"
    assert data["description"] == "Priority"
    assert data["company_id"] == "tenant-test"


def test_tag_name_unique_per_tenant(client: TestClient):
    r1 = client.post("/api/tags", json={"name": "Urgent"})
    assert r1.status_code == 201
    r2 = client.post("/api/tags", json={"name": "Urgent"})
    assert r2.status_code in (409, 422)


def test_color_must_be_hex(client: TestClient):
    r = client.post("/api/tags", json={"name": "Bad", "color": "red"})
    assert r.status_code == 422


def test_assign_and_list_on_job(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Rush"}).json()
    job_id = str(uuid4())
    r = client.post(f"/api/jobs/{job_id}/tags", json={"tag_id": tag["id"]})
    assert r.status_code == 201, r.text

    listed = client.get(f"/api/jobs/{job_id}/tags").json()
    assert len(listed) == 1
    assert listed[0]["id"] == tag["id"]
    assert listed[0]["name"] == "Rush"


def test_assign_idempotent(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Repeat"}).json()
    job_id = str(uuid4())
    r1 = client.post(f"/api/jobs/{job_id}/tags", json={"tag_id": tag["id"]})
    r2 = client.post(f"/api/jobs/{job_id}/tags", json={"tag_id": tag["id"]})
    assert r1.status_code == 201
    assert r2.status_code in (201, 200)
    listed = client.get(f"/api/jobs/{job_id}/tags").json()
    assert len(listed) == 1


def test_unassign(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Temp"}).json()
    job_id = str(uuid4())
    client.post(f"/api/jobs/{job_id}/tags", json={"tag_id": tag["id"]})

    r = client.delete(f"/api/jobs/{job_id}/tags/{tag['id']}")
    assert r.status_code == 204

    listed = client.get(f"/api/jobs/{job_id}/tags").json()
    assert listed == []

    # second delete is 404
    r2 = client.delete(f"/api/jobs/{job_id}/tags/{tag['id']}")
    assert r2.status_code == 404


def test_assign_and_unassign_on_customer(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Gold"}).json()
    cid = str(uuid4())
    r = client.post(f"/api/customers/{cid}/tags", json={"tag_id": tag["id"]})
    assert r.status_code == 201
    assert len(client.get(f"/api/customers/{cid}/tags").json()) == 1

    r2 = client.delete(f"/api/customers/{cid}/tags/{tag['id']}")
    assert r2.status_code == 204
    assert client.get(f"/api/customers/{cid}/tags").json() == []


def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        tag_a = c1.post("/api/tags", json={"name": "Alpha"}).json()
        c2.post("/api/tags", json={"name": "Bravo"})

        list_a = c1.get("/api/tags").json()
        list_b = c2.get("/api/tags").json()
        assert [t["name"] for t in list_a] == ["Alpha"]
        assert [t["name"] for t in list_b] == ["Bravo"]

        # Tenant B cannot patch tenant A's tag
        cross_patch = c2.patch(f"/api/tags/{tag_a['id']}", json={"name": "Hacked"})
        assert cross_patch.status_code == 404

        # Tenant B cannot assign tenant A's tag to its own job
        job_b = str(uuid4())
        cross_assign = c2.post(
            f"/api/jobs/{job_b}/tags", json={"tag_id": tag_a["id"]}
        )
        assert cross_assign.status_code == 404
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_soft_delete_tag(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Disposable"}).json()
    job_id = str(uuid4())
    client.post(f"/api/jobs/{job_id}/tags", json={"tag_id": tag["id"]})

    r = client.delete(f"/api/tags/{tag['id']}")
    assert r.status_code == 204

    # No longer in list
    assert all(t["id"] != tag["id"] for t in client.get("/api/tags").json())

    # Assignment was removed along with the tag
    assert client.get(f"/api/jobs/{job_id}/tags").json() == []

    # Patching a soft-deleted tag returns 404
    r2 = client.patch(f"/api/tags/{tag['id']}", json={"name": "Back"})
    assert r2.status_code == 404


def test_patch_updates_tag(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Initial", "color": "#111111"}).json()
    r = client.patch(
        f"/api/tags/{tag['id']}",
        json={"name": "Renamed", "color": "#222222", "description": "Updated"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "Renamed"
    assert data["color"] == "#222222"
    assert data["description"] == "Updated"


def test_patch_rejects_bad_color(client: TestClient):
    tag = client.post("/api/tags", json={"name": "Colorful"}).json()
    r = client.patch(f"/api/tags/{tag['id']}", json={"color": "notahex"})
    assert r.status_code == 422
