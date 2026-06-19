"""SS-30 slice D tests — /api/admin/cutover router."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import cutover_preflight as cp
from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.models.platform_extensions import Base as OutboxBase
from gdx_dispatch.models.platform_ss29_additions import (
    ShadowMigrationCheckpoint,
    ShadowMigrationState,
    SS29Base,
)
from gdx_dispatch.models.platform_ss30_additions import (
    CutoverSchedule,
    SS30Base,
)
from gdx_dispatch.routers.cutover import router


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS29Base.metadata.create_all(engine)
    SS30Base.metadata.create_all(engine)
    OutboxBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customers_v1 (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE customers_v2 (id INTEGER PRIMARY KEY)"))
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _now():
    return datetime.now(timezone.utc)


def _app(db, *, role="super-admin"):
    app = FastAPI()

    # Router still reads ``request.state.db`` for the DB handle; principal
    # resolution now flows through the composite dispatcher dep.
    @app.middleware("http")
    async def inject_db(request: Request, call_next):
        request.state.db = db
        return await call_next(request)

    def _fake_principal():
        return SimpleNamespace(
            identity_id="u-super",
            tenant_id=None,
            principal_role=role,
            capabilities=(),
            is_super_admin=False,
        )

    app.dependency_overrides[get_current_principal] = _fake_principal
    app.include_router(router)
    return app


def _seed_shadow(db, tenant="t1", table="customers_v1"):
    state = ShadowMigrationState(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        new_table=table.replace("_v1", "_v2"),
        mode="shadow",
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(state)
    cp_row = ShadowMigrationCheckpoint(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        last_row_id=100,
        last_row_pk="100",
        row_count_this_session=100,
        updated_at=_now(),
    )
    db.add(cp_row)
    db.flush()
    db.commit()
    return state


def test_preflight_requires_super_admin(db):
    _seed_shadow(db)
    c = TestClient(_app(db, role="admin"))
    r = c.post("/api/admin/cutover/customers_v1/preflight", json={"tenant_id": "t1"})
    assert r.status_code == 403


def test_preflight_happy_path(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))
    _seed_shadow(db)
    c = TestClient(_app(db))
    r = c.post("/api/admin/cutover/customers_v1/preflight", json={"tenant_id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert len(body["checks"]) == 4


def test_preflight_missing_tenant(db):
    c = TestClient(_app(db))
    r = c.post("/api/admin/cutover/customers_v1/preflight", json={})
    assert r.status_code == 400


def test_execute_requires_preflight_pass(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (False, 2))
    _seed_shadow(db)
    c = TestClient(_app(db))
    r = c.post("/api/admin/cutover/customers_v1/execute", json={"tenant_id": "t1"})
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["error"] == "preflight_failed"


def test_execute_happy_path(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))
    _seed_shadow(db)
    c = TestClient(_app(db))
    r = c.post(
        "/api/admin/cutover/customers_v1/execute",
        json={"tenant_id": "t1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["dry_run"] is False
    assert body["result"]["deprecated_table"] == "customers_v1_v1_deprecated"


def test_execute_dry_run(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))
    _seed_shadow(db)
    c = TestClient(_app(db))
    r = c.post(
        "/api/admin/cutover/customers_v1/execute",
        json={"tenant_id": "t1", "dry_run": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["dry_run"] is True
    # No schedule row persisted on dry run.
    assert db.query(CutoverSchedule).count() == 0


def test_execute_skip_preflight(db):
    _seed_shadow(db)
    c = TestClient(_app(db))
    r = c.post(
        "/api/admin/cutover/customers_v1/execute",
        json={"tenant_id": "t1", "skip_preflight": True},
    )
    assert r.status_code == 200, r.text


def test_extend_deprecation_requires_days(db):
    c = TestClient(_app(db))
    r = c.post(
        "/api/admin/cutover/customers_v1/extend-deprecation",
        json={"tenant_id": "t1"},
    )
    assert r.status_code == 400


def test_extend_deprecation_no_schedule_row(db):
    c = TestClient(_app(db))
    r = c.post(
        "/api/admin/cutover/customers_v1/extend-deprecation",
        json={"tenant_id": "t1", "additional_days": 5},
    )
    assert r.status_code == 404


def test_extend_deprecation_happy(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))
    _seed_shadow(db)
    c = TestClient(_app(db))
    c.post(
        "/api/admin/cutover/customers_v1/execute",
        json={"tenant_id": "t1"},
    )
    r = c.post(
        "/api/admin/cutover/customers_v1/extend-deprecation",
        json={"tenant_id": "t1", "additional_days": 10},
    )
    assert r.status_code == 200, r.text
    assert r.json()["extended_count"] == "1"


def test_extend_deprecation_exceeds_cap(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))
    _seed_shadow(db)
    c = TestClient(_app(db))
    c.post(
        "/api/admin/cutover/customers_v1/execute",
        json={"tenant_id": "t1"},
    )
    r = c.post(
        "/api/admin/cutover/customers_v1/extend-deprecation",
        json={"tenant_id": "t1", "additional_days": 400},
    )
    assert r.status_code == 409


def test_status_before_cutover(db):
    _seed_shadow(db)
    c = TestClient(_app(db))
    r = c.get("/api/admin/cutover/customers_v1/status?tenant_id=t1")
    assert r.status_code == 200
    body = r.json()
    assert body["state"]["mode"] == "shadow"
    assert body["schedule"] is None


def test_status_after_cutover(db, monkeypatch):
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))
    _seed_shadow(db)
    c = TestClient(_app(db))
    c.post("/api/admin/cutover/customers_v1/execute", json={"tenant_id": "t1"})
    r = c.get("/api/admin/cutover/customers_v1/status?tenant_id=t1")
    assert r.status_code == 200
    body = r.json()
    assert body["state"]["mode"] == "cutover"
    assert body["schedule"]["deprecated_table"] == "customers_v1_v1_deprecated"


def test_status_missing_tenant_id(db):
    c = TestClient(_app(db))
    r = c.get("/api/admin/cutover/customers_v1/status")
    assert r.status_code == 400


def test_cutover_state_survives_mock_restart(monkeypatch):
    """Sprint 0.9-j: schedule a cutover, close+reopen the DB session, and
    confirm the schedule row is still there via the real /status route.

    This is the smoking-gun test that the cutover router persists through
    the ORM (``cutover_schedule`` table) rather than any module-level
    in-memory dict — a restart of the session MUST NOT lose state.
    """
    monkeypatch.setattr(cp, "verify_chain", lambda *a, **kw: (True, -1))

    # Engine outlives the "process": StaticPool keeps the SQLite in-memory
    # DB alive across session open/close cycles so we can simulate a
    # worker-process restart without losing on-disk state.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS29Base.metadata.create_all(engine)
    SS30Base.metadata.create_all(engine)
    OutboxBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customers_v1 (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE customers_v2 (id INTEGER PRIMARY KEY)"))
    S = sessionmaker(bind=engine)

    # --- Session #1: seed shadow state + execute cutover via the router ---
    s1 = S()
    try:
        _seed_shadow(s1)
        c1 = TestClient(_app(s1))
        r = c1.post(
            "/api/admin/cutover/customers_v1/execute",
            json={"tenant_id": "t1"},
        )
        assert r.status_code == 200, r.text
        s1.commit()
    finally:
        s1.close()

    # --- Simulated restart: fresh session, new TestClient, same engine ---
    s2 = S()
    try:
        # The ORM query must find the row that session #1 persisted.
        sched_rows = s2.query(CutoverSchedule).all()
        assert len(sched_rows) == 1, "schedule row should survive session restart"
        assert sched_rows[0].deprecated_table == "customers_v1_v1_deprecated"

        # And the router's /status route must see it through the ORM path.
        c2 = TestClient(_app(s2))
        r = c2.get("/api/admin/cutover/customers_v1/status?tenant_id=t1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schedule"] is not None
        assert body["schedule"]["deprecated_table"] == "customers_v1_v1_deprecated"
        assert body["state"]["mode"] == "cutover"
    finally:
        s2.close()
        engine.dispose()
