"""Graceful degradation + debug recording when cc_support_tickets is absent.

cc_support_tickets is provisioned by the Command Center's alembic, not this app.
On a DB where it's missing, the support endpoints must degrade (empty list / 503)
rather than surfacing a raw 500. When the operator turns on debug logging
(app_settings.debug_logging_enabled), the otherwise-swallowed error is also
recorded to the server-error sink so it shows on the Server Errors page.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import support as support_router


@pytest.fixture()
def ctx():
    # create_all builds app_settings (+ other ORM tables) but NOT
    # cc_support_tickets (control-plane, no ORM model) — exactly the drift we
    # want to exercise.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
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
    yield tc, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def _set_debug(SessionLocal, enabled: bool):
    db = SessionLocal()
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(company_name="t")
        db.add(row)
    row.debug_logging_enabled = enabled
    db.commit()
    db.close()


def test_my_tickets_returns_empty_when_table_absent(ctx):
    tc, _ = ctx
    r = tc.get("/api/support/my")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}


def test_submit_bug_returns_503_when_table_absent(ctx):
    tc, _ = ctx
    r = tc.post("/api/support/bug", json={"subject": "Broken page", "body": "It does not load.", "priority": "medium"})
    assert r.status_code == 503, r.text
    assert "unavailable" in r.json()["detail"].lower()


def test_no_sink_recording_when_debug_off(ctx, monkeypatch):
    tc, SL = ctx
    _set_debug(SL, False)
    calls = []
    monkeypatch.setattr("gdx_dispatch.modules.error_sink.record_server_error", lambda **kw: calls.append(kw))
    tc.get("/api/support/my")
    assert calls == []


def test_records_to_sink_when_debug_on(ctx, monkeypatch):
    tc, SL = ctx
    _set_debug(SL, True)
    calls = []
    monkeypatch.setattr("gdx_dispatch.modules.error_sink.record_server_error", lambda **kw: calls.append(kw))
    # GET degrades to empty AND records
    r = tc.get("/api/support/my")
    assert r.status_code == 200
    assert r.json() == {"items": []}
    # POST 503s AND records
    r2 = tc.post("/api/support/bug", json={"subject": "Broken page", "body": "Will not load.", "priority": "high"})
    assert r2.status_code == 503
    assert len(calls) == 2
    # Recorded as 503 (support subsystem unavailable), not 500 — the GET still
    # returned 200 to the client, so a logged 500 would be misleading.
    assert all(c["status_code"] == 503 for c in calls)


def test_missing_table_helper_classifies_precisely():
    from sqlalchemy.exc import OperationalError, ProgrammingError

    class _Orig(Exception):
        def __init__(self, msg, pgcode=None):
            super().__init__(msg)
            self.pgcode = pgcode

    pg_missing_table = ProgrammingError("stmt", {}, _Orig('relation "cc_support_tickets" does not exist', pgcode="42P01"))
    sqlite_missing_table = OperationalError("stmt", {}, _Orig("no such table: cc_support_tickets"))
    assert support_router._is_missing_table(pg_missing_table) is True
    assert support_router._is_missing_table(sqlite_missing_table) is True

    pg_missing_column = ProgrammingError("stmt", {}, _Orig('column "foo" does not exist', pgcode="42703"))
    sqlite_missing_column = OperationalError("stmt", {}, _Orig("no such column: foo"))
    db_locked = OperationalError("stmt", {}, _Orig("database is locked"))
    assert support_router._is_missing_table(pg_missing_column) is False
    assert support_router._is_missing_table(sqlite_missing_column) is False
    assert support_router._is_missing_table(db_locked) is False
