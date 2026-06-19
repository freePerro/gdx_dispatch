"""Tests for the in-app tour progress router."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.tours import router


_TEST_USER_ID = str(uuid4())
_TEST_TENANT_ID = str(uuid4())


def _make_client(user_id: str = _TEST_USER_ID, create_table: bool = True) -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if create_table:
        TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": _TEST_TENANT_ID}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user_id,
        "sub": user_id,
        "role": "admin",
        "tenant_id": _TEST_TENANT_ID,
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


def test_list_tours_empty_for_new_user(client: TestClient):
    r = client.get("/api/me/tours")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["progress"] == {}
    assert body["available"] is True


def test_start_tour_persists(client: TestClient):
    r = client.post("/api/me/tours/owner-getting-started/start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tour_id"] == "owner-getting-started"
    assert body["status"] == "started"
    assert body["version"] == 1

    r2 = client.get("/api/me/tours")
    assert r2.status_code == 200
    progress = r2.json()["progress"]
    assert "owner-getting-started" in progress
    assert progress["owner-getting-started"]["status"] == "started"


def test_complete_tour_sets_completed_at(client: TestClient):
    client.post("/api/me/tours/admin-getting-started/start")
    r = client.post("/api/me/tours/admin-getting-started/complete")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None


def test_skip_tour(client: TestClient):
    r = client.post("/api/me/tours/dispatcher-daily-flow/skip")
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


def test_update_step(client: TestClient):
    client.post("/api/me/tours/tech-mobile-flow/start")
    r = client.post(
        "/api/me/tours/tech-mobile-flow/step",
        json={"step_index": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["last_step"] == 3
    # Status is unchanged by step update.
    assert body["status"] == "started"


def test_invalid_tour_id_rejected(client: TestClient):
    r = client.post("/api/me/tours/BAD ID WITH SPACES/start")
    # FastAPI may URL-encode and pass through; our regex validator rejects.
    assert r.status_code in (400, 404)


def test_idempotent_start(client: TestClient):
    r1 = client.post("/api/me/tours/owner-getting-started/start")
    r2 = client.post("/api/me/tours/owner-getting-started/start")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Same tour_id → same row, no duplicate.
    listing = client.get("/api/me/tours").json()
    assert len(listing["progress"]) == 1


def test_missing_table_returns_unavailable(monkeypatch):
    """If the tenant DB doesn't have the user_tour_progress table yet,
    GET returns 200 with available=False. Lets the frontend treat
    pre-migration tenants as if everyone is on their first tour."""
    tc = _make_client(create_table=False)
    try:
        r = tc.get("/api/me/tours")
        assert r.status_code == 200
        body = r.json()
        assert body["progress"] == {}
        assert body["available"] is False
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_missing_table_post_does_not_crash():
    """POST /start /complete /skip /step against a tenant whose DB
    doesn't yet have the table must return 200 with null body, not 500.
    Otherwise users on un-migrated tenants get a backend error every
    time the tour engine syncs."""
    tc = _make_client(create_table=False)
    try:
        for action in ("start", "complete", "skip"):
            r = tc.post(f"/api/me/tours/some-tour/{action}")
            assert r.status_code == 200, f"{action} failed: {r.text}"
            assert r.json() is None
        r = tc.post("/api/me/tours/some-tour/step", json={"step_index": 0})
        assert r.status_code == 200
        assert r.json() is None
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_version_bump_resets_completed_to_started(client: TestClient):
    """The catalog declares a version per tour. When we ship a tour
    rewrite, bumping the version + sending the new int on /start MUST
    flip a previously completed row back to "started" so the user sees
    the new version. Without this, the version field is theater."""
    # User completes v1.
    client.post("/api/me/tours/owner-getting-started/start", json={"version": 1})
    r = client.post("/api/me/tours/owner-getting-started/complete", json={"version": 1})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    assert r.json()["version"] == 1

    # We ship v2 — client sends version=2 on /start. Row resets.
    r2 = client.post("/api/me/tours/owner-getting-started/start", json={"version": 2})
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "started", "version bump must reset status"
    assert body["version"] == 2
    assert body["completed_at"] is None


def test_version_bump_only_resets_when_newer(client: TestClient):
    """Sending an older version than what's already stored must NOT
    downgrade — that would let a stale client tab clobber a fresher
    record on the same user."""
    client.post("/api/me/tours/admin-getting-started/start", json={"version": 3})
    client.post("/api/me/tours/admin-getting-started/complete", json={"version": 3})

    r = client.post("/api/me/tours/admin-getting-started/start", json={"version": 1})
    body = r.json()
    # version stays at 3, status stays at completed (we only re-fire on UPGRADE).
    assert body["version"] == 3
    assert body["status"] == "completed"
