"""HTTP-level test for GET /api/timeclock/report.

The unit tests in test_03_sprint2_modules.py call daily_labor_report()
directly, which never exercises the role gate. an earlier session found that the
report endpoint was 403'ing every JWT-auth user in prod (admins, dispatchers
— everyone) because the gate was wired to require_min_role, which only
reads request.state.current_user (set only by the service-account
middleware). This route-level test exercises the JWT path that real
clients use, so the same class of bug fails CI next time.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.timeclock.router import router
from gdx_dispatch.routers.auth import get_current_user


def _make_client(role: str) -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS tenant_module_grants ("
        "id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT, "
        "granted_at TEXT, created_at TEXT, expires_at TEXT)"
    ))
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS company_module_grants ("
        "id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT, "
        "granted_at TEXT, created_at TEXT, expires_at TEXT, "
        "UNIQUE(company_id, module_key))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)"
        " VALUES ('g1','tenant-test','timeclock',datetime('now'),datetime('now'))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)"
        " VALUES ('g2','tenant-test','timeclock',datetime('now'),datetime('now'))"
    ))
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
    async def inject(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        request.state.current_user = {
            "user_id": "user-1",
            "role": role,
            "tenant_id": "tenant-test",
        }
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "role": role,
        "tenant_id": "tenant-test",
    }
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.parametrize("role", ["technician", "viewer"])
def test_report_forbidden_for_low_roles(role):
    client = _make_client(role=role)
    r = client.get("/api/timeclock/report?date=2026-05-05")
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["dispatcher", "admin", "owner", "manager", "accounting"])
def test_report_allowed_for_above_tech(role):
    client = _make_client(role=role)
    r = client.get("/api/timeclock/report?date=2026-05-05")
    assert r.status_code == 200
    assert r.json() == []
