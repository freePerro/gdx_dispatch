"""Tests for gdx_dispatch/core/public_api.py — REST API v1 with API key authentication.

All routes require X-API-Key header.  These tests use a minimal FastAPI app
that mounts the public_api router directly, with an in-memory SQLite DB and
a middleware shim that injects tenant_id + scopes from the test fixtures.

Tests (8+):
  test_public_api_requires_api_key      — 401 when no key present
  test_invalid_api_key_rejected         — 401 when middleware rejects bad key
  test_list_jobs_via_api                — 200 + envelope after seeding a job
  test_create_job_via_api               — 201 + job data returned
  test_list_customers_via_api           — 200 + envelope after seeding a customer
  test_list_invoices_via_api            — 200 + envelope after seeding an invoice
  test_register_webhook                 — 201 + webhook data returned
  test_api_key_scoped_to_tenant         — tenant B key cannot see tenant A data
  test_register_webhook_rejects_http    — 422 when url is http://
  test_delete_webhook                   — soft-deletes a registered webhook
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Tenant IDs used throughout
# ---------------------------------------------------------------------------

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())

# ---------------------------------------------------------------------------
# Shared in-memory SQLite engine (StaticPool — same connection for all tests)
# ---------------------------------------------------------------------------

_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def _bootstrap_db():
    with _ENGINE.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Scheduled',
                customer_id TEXT,
                scheduled_at TEXT,
                company_id TEXT NOT NULL,
                deleted_at TEXT,
                created_at TEXT NOT NULL
            )
        """))
        # Schema aligned to the Customer ORM model so ORM queries
        # (introduced in S122-9 slice 3 alongside `address`-as-EncryptedString)
        # don't crash on missing columns. The hash columns + opt-out
        # flags + cached volume + notes_appended are all nullable in
        # the model, so this minimal stub matches.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_hash TEXT,
                email TEXT,
                email_hash TEXT,
                phone TEXT,
                phone_hash TEXT,
                address TEXT,
                metadata TEXT,
                notes TEXT,
                notes_appended TEXT,
                source TEXT,
                customer_type TEXT,
                pricing_class TEXT,
                margin_override_pct REAL,
                payment_terms_days INTEGER,
                cached_rolling_volume_paid_12mo REAL,
                cached_rolling_volume_at TEXT,
                email_opt_out INTEGER,
                sms_opt_out INTEGER,
                qb_dirty INTEGER DEFAULT 1,
                qb_synced_at TEXT,
                updated_at TEXT,
                company_id TEXT NOT NULL,
                deleted_at TEXT,
                created_at TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                customer_id TEXT,
                amount REAL,
                status TEXT DEFAULT 'draft',
                company_id TEXT NOT NULL,
                deleted_at TEXT,
                created_at TEXT NOT NULL
            )
        """))
        # Match the WebhookEndpoint ORM model (post-S122-9 slice 2). The
        # pre-S122-9 fixture had a bespoke `company_id NOT NULL` + `deleted_at`
        # that didn't exist on prod or in the ORM — vestigial from the
        # raw-SQL writer at `public_api.py:395` that was refactored to ORM
        # in slice 2.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS webhook_endpoints (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                events TEXT NOT NULL DEFAULT '[]',
                secret TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """))


_bootstrap_db()

_SessionLocal = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _get_db():
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------

def _app_with_key(tenant_id: str, scopes: list[str]) -> TestClient:
    """Mount public_api router; inject tenant_id/scopes as if API key is valid."""
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.public_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.api_key_tenant_id = tenant_id
        request.state.api_key_scopes = scopes
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def _app_no_key() -> TestClient:
    """Mount public_api router with NO key injected (state.api_key_tenant_id absent)."""
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.public_api import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def _app_reject_key(bad_key: str) -> TestClient:
    """Middleware that returns 401 for a specific key, simulating failed key lookup."""
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.public_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _reject(request, call_next):
        if request.headers.get("X-API-Key") == bad_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired API key"},
            )
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC).isoformat()


def _seed_job(tenant_id: str, title: str = "Test Job") -> str:
    job_id = str(uuid.uuid4())
    db = _SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO jobs (id, title, status, company_id, created_at) "
            "VALUES (:id, :title, 'Scheduled', :tid, :ts)"
        ), {"id": job_id, "title": title, "tid": tenant_id, "ts": _NOW})
        db.commit()
    finally:
        db.close()
    return job_id


def _seed_customer(tenant_id: str, name: str = "Alice Smith") -> str:
    cid = str(uuid.uuid4())
    db = _SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO customers (id, name, company_id, created_at) "
            "VALUES (:id, :name, :tid, :ts)"
        ), {"id": cid, "name": name, "tid": tenant_id, "ts": _NOW})
        db.commit()
    finally:
        db.close()
    return cid


def _seed_invoice(tenant_id: str, amount: float = 199.99) -> str:
    inv_id = str(uuid.uuid4())
    db = _SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO invoices (id, amount, status, company_id, created_at) "
            "VALUES (:id, :amount, 'sent', :tid, :ts)"
        ), {"id": inv_id, "amount": amount, "tid": tenant_id, "ts": _NOW})
        db.commit()
    finally:
        db.close()
    return inv_id


# ===========================================================================
# Tests
# ===========================================================================


def test_public_api_requires_api_key():
    """GET /v1/jobs with no key header returns 401."""
    client = _app_no_key()
    resp = client.get("/v1/jobs")
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_invalid_api_key_rejected():
    """Middleware rejects a bogus key with 401 before the route runs."""
    bad = "gdx_live_totally_invalid_key_xyz"
    client = _app_reject_key(bad)
    resp = client.get("/v1/jobs", headers={"X-API-Key": bad})
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_list_jobs_via_api():
    """GET /v1/jobs with valid key returns envelope with seeded job."""
    _seed_job(TENANT_A, "Garage door spring replacement")
    client = _app_with_key(TENANT_A, ["read:jobs"])
    resp = client.get("/v1/jobs", headers={"X-API-Key": "gdx_live_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)
    assert any(j["title"] == "Garage door spring replacement" for j in body["data"])


def test_create_job_via_api():
    """POST /v1/jobs with valid write:jobs scope returns 201 with job data."""
    client = _app_with_key(TENANT_A, ["read:jobs", "write:jobs"])
    resp = client.post(
        "/v1/jobs",
        json={"title": "Cable repair", "status": "Scheduled"},
        headers={"X-API-Key": "gdx_live_test"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "data" in body
    assert body["data"]["title"] == "Cable repair"
    assert "id" in body["data"]
    assert "meta" in body


def test_list_customers_via_api():
    """GET /v1/customers with valid key returns envelope with seeded customer."""
    _seed_customer(TENANT_A, "Bob Builder")
    client = _app_with_key(TENANT_A, ["read:customers"])
    resp = client.get("/v1/customers", headers={"X-API-Key": "gdx_live_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)
    assert any(c["name"] == "Bob Builder" for c in body["data"])


def test_list_invoices_via_api():
    """GET /v1/invoices with valid read:invoices scope returns envelope with seeded invoice."""
    _seed_invoice(TENANT_A, 349.50)
    client = _app_with_key(TENANT_A, ["read:invoices"])
    resp = client.get("/v1/invoices", headers={"X-API-Key": "gdx_live_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)
    assert any(abs(inv.get("amount", 0) - 349.50) < 0.01 for inv in body["data"])


def test_register_webhook():
    """POST /v1/webhooks with valid write:webhooks scope returns 201 with webhook data."""
    client = _app_with_key(TENANT_A, ["write:webhooks"])
    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://hooks.example.com/gdx", "events": ["job.created", "job.updated"]},
        headers={"X-API-Key": "gdx_live_test"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "data" in body
    assert body["data"]["url"] == "https://hooks.example.com/gdx"
    assert "id" in body["data"]
    assert "meta" in body


def test_api_key_scoped_to_tenant():
    """Jobs created under Tenant A are not visible when querying as Tenant B."""
    unique_title = f"TenantA-private-{uuid.uuid4().hex[:8]}"
    _seed_job(TENANT_A, unique_title)

    client_b = _app_with_key(TENANT_B, ["read:jobs"])
    resp = client_b.get("/v1/jobs", headers={"X-API-Key": "gdx_live_tenant_b"})
    assert resp.status_code == 200
    titles = [j["title"] for j in resp.json().get("data", [])]
    assert unique_title not in titles, (
        f"Tenant isolation breach: Tenant B can see Tenant A job '{unique_title}'"
    )


def test_register_webhook_rejects_http():
    """POST /v1/webhooks with an http:// url returns 422."""
    client = _app_with_key(TENANT_A, ["write:webhooks"])
    resp = client.post(
        "/v1/webhooks",
        json={"url": "http://insecure.example.com/hook", "events": ["job.created"]},
        headers={"X-API-Key": "gdx_live_test"},
    )
    assert resp.status_code == 422


def test_delete_webhook():
    """DELETE /v1/webhooks/{id} soft-deletes a previously registered webhook."""
    client = _app_with_key(TENANT_A, ["write:webhooks"])
    # Register first
    create_resp = client.post(
        "/v1/webhooks",
        json={"url": "https://delete-me.example.com/hook", "events": ["job.completed"]},
        headers={"X-API-Key": "gdx_live_test"},
    )
    assert create_resp.status_code == 201
    webhook_id = create_resp.json()["data"]["id"]

    # Now delete it
    del_resp = client.delete(
        f"/v1/webhooks/{webhook_id}",
        headers={"X-API-Key": "gdx_live_test"},
    )
    assert del_resp.status_code == 200
    body = del_resp.json()
    assert body["data"]["ok"] is True
    assert body["data"]["id"] == webhook_id
