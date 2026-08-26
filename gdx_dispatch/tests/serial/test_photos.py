"""Tests for the photos router (job photo gallery + recent feed)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.models.tenant_models import JobPhoto
from gdx_dispatch.routers.photos import router


def _make_client(
    tenant_id: str = "tenant-test",
    user_sub: str = "user-1",
    user_role: str = "dispatcher",
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



def _seed_photo(client: TestClient, job_id: str, *, tenant_id: str = "tenant-test", **kw) -> dict:
    """Insert a JobPhoto row directly, the way a real upload would leave one.

    These tests used to create photos by POSTing JSON to
    /api/jobs/{job_id}/photos. That handler was deleted 2026-08-26 (#489)
    because it never served a request in the real app: routers/uploads.py
    declares the same path as MULTIPART and is included first, so the JSON one
    was permanently shadowed and every call 422'd.

    It passed HERE only because this suite builds a bare FastAPI() including
    photos.router ALONE (see _make_client), where nothing shadowed it. That is
    the trap worth naming: a green test against a route production could never
    reach. Seeding the row directly tests the same GET / PATCH / DELETE surface
    without depending on a writer that does not exist.
    """
    Session = sessionmaker(bind=client._engine, autoflush=False, autocommit=False)  # type: ignore[attr-defined]
    db = Session()
    try:
        photo = JobPhoto(
            company_id=tenant_id,
            job_id=UUID(job_id),
            kind=kw.get("kind", "during"),
            url=kw.get("url", "https://cdn.example.com/a.jpg"),
            filename=kw.get("filename"),
            mime_type=kw.get("mime_type"),
            size_bytes=kw.get("size_bytes"),
            caption=kw.get("caption"),
            uploaded_by=kw.get("uploaded_by", "user-1"),
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        return {"id": str(photo.id), "url": photo.url, "kind": photo.kind, "caption": photo.caption}
    finally:
        db.close()


def test_no_json_writer_is_registered_on_the_photos_router(client: TestClient):
    """This replaces test_create_photo, which POSTed JSON to
    /api/jobs/{job_id}/photos and asserted 201.

    That route was deleted (#489). It never served a request in the real app —
    routers/uploads.py claims the same path as multipart and is included first,
    so the JSON handler was shadowed and every call 422'd. It passed here only
    because this suite mounts photos.router alone.

    Absence is now the property worth guarding: the photos router must not grow
    a second writer for this path. The real writer is
    POST /api/documents (job_id + as_photo=true).
    """
    posts = [
        r for r in client.app.routes
        if getattr(r, "path", "") == "/api/jobs/{job_id}/photos"
        and "POST" in (getattr(r, "methods", None) or set())
    ]
    assert posts == [], f"photos.router grew a POST writer again: {posts}"


def test_list_photos_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        job_id = str(uuid4())
        _seed_photo(c1, job_id, tenant_id="tenant-a", url="https://cdn.example.com/a.jpg", kind="during")
        _seed_photo(c2, job_id, tenant_id="tenant-b", url="https://cdn.example.com/b.jpg", kind="after")

        list1 = c1.get(f"/api/jobs/{job_id}/photos").json()
        list2 = c2.get(f"/api/jobs/{job_id}/photos").json()
        assert len(list1) == 1 and list1[0]["url"] == "https://cdn.example.com/a.jpg"
        assert len(list2) == 1 and list2[0]["url"] == "https://cdn.example.com/b.jpg"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_kind_is_normalised_by_the_shared_helper():
    """Was: POST an invalid kind to the deleted JSON route, expect 422.

    That validation lived on a pydantic model behind a route production could
    not reach. The check that actually runs on every real upload is
    core/job_photos.normalize_kind, which coerces rather than rejects — because
    the column is String(20) and an over-long value raises on flush, the
    savepoint swallows it, and the photo vanishes silently.
    """
    from gdx_dispatch.core.job_photos import DEFAULT_PHOTO_KIND, PHOTO_KINDS, normalize_kind

    assert normalize_kind("before") == "before"
    assert normalize_kind("bogus") == DEFAULT_PHOTO_KIND
    assert normalize_kind(None) == DEFAULT_PHOTO_KIND
    assert normalize_kind(12345) == DEFAULT_PHOTO_KIND
    assert normalize_kind("x" * 400) == DEFAULT_PHOTO_KIND
    for kind in PHOTO_KINDS:
        assert normalize_kind(kind) == kind


def test_recent_photos_feed(client: TestClient):
    job_id = str(uuid4())
    for i in range(3):
        _seed_photo(client, job_id, url=f"https://cdn.example.com/{i}.jpg",
                    kind="during", caption=f"shot-{i}")

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
    created = _seed_photo(client, job_id, url="https://cdn.example.com/a.jpg", kind="during")
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
    created = _seed_photo(client, job_id, url="https://cdn.example.com/a.jpg", kind="before")
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
