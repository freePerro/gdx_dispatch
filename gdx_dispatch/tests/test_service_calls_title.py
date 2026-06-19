"""Regression: POST /api/service-calls should set Job.title to the customer name,
not "Service Call — <name>" (the type is conveyed by Job.job_type)."""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.service_calls import router


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
        VALUES ('g1', 'tenant-test', 'jobs', datetime('now'), datetime('now'))
    """))
    setup.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'jobs', datetime('now'), datetime('now'))
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
        "user_id": "user-1",
        "role": "admin",
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, Session
    app.dependency_overrides.clear()
    engine.dispose()


def test_service_call_title_is_customer_name_only(client):
    tc, Session = client
    r = tc.post(
        "/api/service-calls",
        json={
            "customer_name": "Becky Meinecke",
            "problem_description": "Spring broken on north door",
            "urgency": "urgent",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    job_id = body["job_id"]

    db = Session()
    try:
        job = db.execute(select(Job).where(Job.id == UUID(job_id))).scalar_one()
        assert job.title == "Becky Meinecke", (
            f"Title should be the bare customer name, got {job.title!r}"
        )
        assert job.job_type == "Service Call", "job_type still carries the type label"
        assert not job.title.startswith("Service Call"), (
            "Title must not duplicate the type prefix that the badge already shows"
        )
    finally:
        db.close()
