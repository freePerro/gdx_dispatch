"""MH-7b regression — clock-in with a stale (>MAX_SHIFT_HOURS) open shift
must auto-close the old one, audit-log it, and proceed with the new clock-in.

Pre-fix (mobile walk 2026-06-04): backend accepted indefinite open shifts.
Auditor session at 781:14:00 was the trigger. The new behavior is: at
clock-in, if an existing open shift exceeds MAX_SHIFT_HOURS, close it as
`timeclock_auto_close` (audit-logged) and continue. Existing-but-fresh
open shifts still 400 with "Technician already clocked in".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import MAX_SHIFT_HOURS, router


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    setup.execute(text("""
        INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES ('g1', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))
    """))
    setup.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))
    """))
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
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "tech-1",
        "sub": "tech-1",
        "role": "technician",
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_open_entry(Session, *, hours_ago: float) -> str:
    """Insert an open TimeclockEntry whose clock_in_at is `hours_ago` hours ago."""
    clock_in = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    entry_id = f"entry-{int(hours_ago * 100)}"
    db = Session()
    try:
        entry = TimeclockEntry(
            id=entry_id,
            tenant_id="tenant-test",
            technician_id="tech-1",
            clock_in_at=clock_in.isoformat(),
            clock_out_at=None,
            minutes=None,
            notes="stale shift",
            entry_type="clock",
            created_at=clock_in.isoformat(),
            updated_at=clock_in.isoformat(),
        )
        db.add(entry)
        db.commit()
        return entry_id
    finally:
        db.close()


def test_fresh_open_shift_still_blocks_clock_in(client):
    """A shift open less than MAX_SHIFT_HOURS still 400s — only stale
    shifts get the auto-close treatment."""
    tc_client, Session = client
    _seed_open_entry(Session, hours_ago=4.0)
    r = tc_client.post("/api/timeclock/clock-in", json={"technician_id": "tech-1"})
    assert r.status_code == 400, r.text
    assert "already clocked in" in r.text.lower()


def test_stale_open_shift_auto_closes_and_new_clock_in_succeeds(client):
    """A shift open longer than MAX_SHIFT_HOURS at clock-in time should
    be closed (stamped clock_out_at + minutes) and the new clock-in
    should land normally."""
    tc_client, Session = client
    stale_id = _seed_open_entry(Session, hours_ago=MAX_SHIFT_HOURS + 5)

    r = tc_client.post("/api/timeclock/clock-in", json={"technician_id": "tech-1"})
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    assert new_id != stale_id

    db = Session()
    try:
        closed = db.execute(
            select(TimeclockEntry).where(TimeclockEntry.id == stale_id)
        ).scalar_one()
        assert closed.clock_out_at is not None, "stale entry must be closed"
        assert closed.minutes is not None and closed.minutes > 0
        # New shift should be open.
        fresh = db.execute(
            select(TimeclockEntry).where(TimeclockEntry.id == new_id)
        ).scalar_one()
        assert fresh.clock_out_at is None
    finally:
        db.close()


def test_stale_shift_auto_close_emits_audit_log(client):
    """The auto-close must leave a `timeclock_auto_close` audit row so
    payroll review has a trail for who got force-closed and when."""
    tc_client, Session = client
    _seed_open_entry(Session, hours_ago=MAX_SHIFT_HOURS + 2)
    r = tc_client.post("/api/timeclock/clock-in", json={"technician_id": "tech-1"})
    assert r.status_code == 201, r.text

    db = Session()
    try:
        rows = db.execute(
            text(
                "SELECT action, entity_type FROM audit_logs "
                "WHERE action = 'timeclock_auto_close'"
            )
        ).mappings().all()
        assert len(rows) >= 1, "auto-close must emit an audit row"
        assert rows[0]["entity_type"] == "timeclock_entry"
    finally:
        db.close()
