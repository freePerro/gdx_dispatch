"""Tests for the exports router (CSV + JSON bulk export)."""
from __future__ import annotations

from datetime import datetime, timezone
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
from gdx_dispatch.routers.exports import router


def _make_client(
    tenant_id: str = "tenant-test",
    *,
    role: str = "admin",
    seed_customers: bool = True,
    create_tables: bool = True,
) -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()

    # Module grant table + grant "customers"
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
    for mod_key in ("customers", "jobs", "invoices", "estimates"):
        setup.execute(
            text(
                """
                INSERT OR IGNORE INTO company_module_grants
                    (id, company_id, module_key, granted_at, created_at)
                VALUES (:id, :tid, :mk, datetime('now'), datetime('now'))
                """
            ),
            {"id": f"g-{tenant_id}-{mod_key}", "tid": tenant_id, "mk": mod_key},
        )

    if create_tables:
        # Drop ORM-created domain tables so we can recreate them with the
        # extra columns (city, state, zip) that the exports router queries.
        setup.execute(text("DROP TABLE IF EXISTS customers"))
        setup.execute(text("DROP TABLE IF EXISTS jobs"))
        # Domain tables (minimal columns required by exports queries)
        setup.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    name TEXT,
                    email TEXT,
                    phone TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    created_at TEXT,
                    deleted_at TEXT
                )
                """
            )
        )
        setup.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    customer_id TEXT,
                    title TEXT,
                    status TEXT,
                    lifecycle_stage TEXT,
                    total REAL,
                    scheduled_at TEXT,
                    completed_at TEXT,
                    created_at TEXT
                )
                """
            )
        )

    if seed_customers and create_tables:
        setup.execute(
            text(
                """
                INSERT INTO customers (id, company_id, name, email, phone,
                    address, city, state, zip, created_at)
                VALUES (:id, :tid, :name, :email, '555-1111',
                    '1 Main', 'Denver', 'CO', '80202', :now)
                """
            ),
            {
                "id": str(uuid4()),
                "tid": tenant_id,
                "name": f"Cust-{tenant_id}",
                "email": f"cust@{tenant_id}.test",
                "now": datetime.now(timezone.utc).isoformat(),
            },
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
        "role": role,
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


def test_export_customers_returns_csv_with_header(client: TestClient):
    r = client.get("/api/exports/customers")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text.splitlines()
    assert body, "expected at least a header row"
    header = body[0].split(",")
    assert "id" in header
    assert "name" in header
    assert "email" in header


def test_export_customers_content_disposition_attachment(client: TestClient):
    r = client.get("/api/exports/customers")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename=" in cd
    assert ".csv" in cd


def test_export_jobs_accepts_date_range(client: TestClient):
    r = client.get(
        "/api/exports/jobs",
        params={"start": "2024-01-01", "end": "2030-12-31"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    first_line = r.text.splitlines()[0]
    assert "customer_name" in first_line
    assert "lifecycle_stage" in first_line


def test_export_jobs_rejects_bad_date(client: TestClient):
    r = client.get("/api/exports/jobs", params={"start": "not-a-date"})
    assert r.status_code == 422


def test_export_all_returns_json_with_requested_entities(client: TestClient):
    r = client.get("/api/exports/all", params={"entities": "customers,jobs"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "customers" in data
    assert "jobs" in data
    assert isinstance(data["customers"], list)
    assert len(data["customers"]) >= 1
    # Row should be a dict with the CSV header keys
    first = data["customers"][0]
    assert "id" in first and "name" in first and "email" in first


def test_export_all_rejects_unknown_entity(client: TestClient):
    r = client.get("/api/exports/all", params={"entities": "customers,hackers"})
    assert r.status_code == 422


def test_export_missing_table_returns_empty_csv():
    # Engine with NO domain tables — only module grants.
    tc = _make_client(
        tenant_id="tenant-empty", seed_customers=False, create_tables=False
    )
    try:
        r = tc.get("/api/exports/customers")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        lines = r.text.splitlines()
        # Only the header row, no data
        assert len(lines) == 1
        assert "id" in lines[0]
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.get("/api/exports/customers")
        r2 = c2.get("/api/exports/customers")
        assert r1.status_code == 200
        assert r2.status_code == 200
        body1 = r1.text
        body2 = r2.text
        # Each fixture seeds 1 customer named after its tenant.
        assert "Cust-tenant-a" in body1
        assert "Cust-tenant-a" not in body2
        assert "Cust-tenant-b" in body2
        assert "Cust-tenant-b" not in body1
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]
