"""Graceful degradation when the control-plane cc_support_tickets table is absent.

cc_support_tickets is provisioned by the Command Center's alembic, not this app.
On a DB where it's missing, the support endpoints must degrade (empty list / 503)
rather than surfacing a raw 500 — the /feedback page calls /my on load.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.routers import support as support_router


@pytest.fixture()
def client():
    # Bare in-memory DB with NO cc_support_tickets table (mirrors a DB where the
    # CC migrations haven't run).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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

    app.include_router(support_router.router)
    app.dependency_overrides[support_router.get_db] = _override_db
    app.dependency_overrides[support_router.get_current_user] = lambda: {
        "sub": "u1", "email": "u1@example.com", "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def test_my_tickets_returns_empty_when_table_absent(client):
    r = client.get("/api/support/my")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}


def test_submit_bug_returns_503_when_table_absent(client):
    r = client.post("/api/support/bug", json={"subject": "Broken page", "body": "It does not load.", "priority": "medium"})
    assert r.status_code == 503, r.text
    assert "unavailable" in r.json()["detail"].lower()


def test_missing_table_helper_classifies_precisely():
    from sqlalchemy.exc import OperationalError, ProgrammingError

    class _Orig(Exception):
        def __init__(self, msg, pgcode=None):
            super().__init__(msg)
            self.pgcode = pgcode

    # missing TABLE — should degrade
    pg_missing_table = ProgrammingError("stmt", {}, _Orig('relation "cc_support_tickets" does not exist', pgcode="42P01"))
    sqlite_missing_table = OperationalError("stmt", {}, _Orig("no such table: cc_support_tickets"))
    assert support_router._is_missing_table(pg_missing_table) is True
    assert support_router._is_missing_table(sqlite_missing_table) is True

    # missing COLUMN / other drift — must NOT be swallowed (real bug must surface)
    pg_missing_column = ProgrammingError("stmt", {}, _Orig('column "foo" does not exist', pgcode="42703"))
    sqlite_missing_column = OperationalError("stmt", {}, _Orig("no such column: foo"))
    db_locked = OperationalError("stmt", {}, _Orig("database is locked"))
    assert support_router._is_missing_table(pg_missing_column) is False
    assert support_router._is_missing_table(sqlite_missing_column) is False
    assert support_router._is_missing_table(db_locked) is False
