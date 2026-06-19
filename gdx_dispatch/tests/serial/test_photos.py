"""Tests for the photos router (job photo gallery + recent feed)."""
from __future__ import annotations

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
from gdx_dispatch.routers.photos import router


def _make_client(
    tenant_id: str = "tenant-test",
    user_sub: str = "user-1",
    user_role: str = "technician",
    engine=None,
) -> TestClient:
    if engine is None:
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
        "user_id": user_sub,
        "sub": user_sub,
        "role": user_role,
        "tenant_id": tenant_id,
        "email": f"{user_sub}@example.com",
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


def test_create_photo(client: TestClient):
    job_id = str(uuid4())
    r = client.post(
        f"/api/jobs/{job_id}/photos",
        json={
            "url": "https://cdn.example.com/a.jpg",
            "kind": "before",
            "filename": "a.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 12345,
            "caption": "Arrival shot",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["job_id"] == job_id
    assert data["kind"] == "before"
    assert data["url"] == "https://cdn.example.com/a.jpg"
    assert data["filename"] == "a.jpg"
    assert data["mime_type"] == "image/jpeg"
    assert data["size_bytes"] == 12345
    assert data["caption"] == "Arrival shot"
    assert data["company_id"] == "tenant-test"


def test_list_photos_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        job_id = str(uuid4())
        r1 = c1.post(
            f"/api/jobs/{job_id}/photos",
            json={"url": "https://cdn.example.com/a.jpg", "kind": "during"},
        )
        assert r1.status_code == 201
        r2 = c2.post(
            f"/api/jobs/{job_id}/photos",
            json={"url": "https://cdn.example.com/b.jpg", "kind": "after"},
        )
        assert r2.status_code == 201

        list1 = c1.get(f"/api/jobs/{job_id}/photos").json()
        list2 = c2.get(f"/api/jobs/{job_id}/photos").json()
        assert len(list1) == 1 and list1[0]["url"] == "https://cdn.example.com/a.jpg"
        assert len(list2) == 1 and list2[0]["url"] == "https://cdn.example.com/b.jpg"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_kind_must_be_valid(client: TestClient):
    job_id = str(uuid4())
    r = client.post(
        f"/api/jobs/{job_id}/photos",
        json={"url": "https://cdn.example.com/a.jpg", "kind": "bogus"},
    )
    assert r.status_code == 422


def test_recent_photos_feed(client: TestClient):
    job_id = str(uuid4())
    for i in range(3):
        r = client.post(
            f"/api/jobs/{job_id}/photos",
            json={
                "url": f"https://cdn.example.com/{i}.jpg",
                "kind": "during",
                "caption": f"shot-{i}",
            },
        )
        assert r.status_code == 201

    feed = client.get("/api/photos/recent?limit=20")
    assert feed.status_code == 200, feed.text
    items = feed.json()
    assert len(items) == 3
    # Newest first — last inserted (shot-2) should be first
    assert items[0]["caption"] == "shot-2"
    assert items[-1]["caption"] == "shot-0"
    # Each item includes job_id for linking
    for item in items:
        assert item["job_id"] == job_id


def test_patch_kind_and_caption(client: TestClient):
    job_id = str(uuid4())
    created = client.post(
        f"/api/jobs/{job_id}/photos",
        json={"url": "https://cdn.example.com/a.jpg", "kind": "during"},
    ).json()
    r = client.patch(
        f"/api/jobs/{job_id}/photos/{created['id']}",
        json={"kind": "after", "caption": "Finished"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "after"
    assert data["caption"] == "Finished"

    # invalid kind rejected
    bad = client.patch(
        f"/api/jobs/{job_id}/photos/{created['id']}",
        json={"kind": "nope"},
    )
    assert bad.status_code == 422


def test_soft_delete_photo(client: TestClient):
    job_id = str(uuid4())
    created = client.post(
        f"/api/jobs/{job_id}/photos",
        json={"url": "https://cdn.example.com/a.jpg", "kind": "before"},
    ).json()
    r = client.delete(f"/api/jobs/{job_id}/photos/{created['id']}")
    assert r.status_code == 204

    listed = client.get(f"/api/jobs/{job_id}/photos").json()
    assert all(p["id"] != created["id"] for p in listed)

    # Recent feed also excludes soft-deleted
    feed = client.get("/api/photos/recent").json()
    assert all(p["id"] != created["id"] for p in feed)

    # Follow-up patch on deleted = 404
    r2 = client.patch(
        f"/api/jobs/{job_id}/photos/{created['id']}",
        json={"caption": "zzz"},
    )
    assert r2.status_code == 404
