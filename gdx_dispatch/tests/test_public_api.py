"""
gdx_dispatch/tests/test_public_api.py — Tests for the GDX Public REST API v1.

Uses in-memory SQLite for both control and tenant databases.
API key auth is exercised end-to-end: real hash stored, real header sent.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# In-memory DB factories
# ---------------------------------------------------------------------------

TENANT_ID = str(uuid.uuid4())
RAW_API_KEY = "gdx_live_test1234567890abcdef1234567890ab"
API_KEY_HASH = hashlib.sha256(RAW_API_KEY.encode()).hexdigest()
API_KEY_ID = str(uuid.uuid4())

TENANT_DB_URL = "sqlite://"  # per-connection in-memory
CONTROL_DB_URL = "sqlite://"


def _make_control_engine():
    engine = create_engine(
        CONTROL_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Create ORM-defined api_keys table (verify_api_key uses db.query(APIKey))
    try:
        from gdx_dispatch.core.api_keys import APIKeyBase
        APIKeyBase.metadata.create_all(engine, checkfirst=True)
    except Exception:
        pass

    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                slug TEXT,
                subscription_status TEXT,
                db_provisioned INTEGER DEFAULT 0,
                deleted_at TEXT
            )
            """
        ))
        # Ensure api_keys exists even if ORM creation failed
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL DEFAULT '',
                name TEXT,
                scopes TEXT DEFAULT '[]',
                last_used_at TEXT,
                created_at TEXT,
                expires_at TEXT,
                revoked_at TEXT
            )
            """
        ))
        # Insert test tenant
        conn.execute(text(
            """
            INSERT OR IGNORE INTO tenants (id, slug, subscription_status, db_provisioned)
            VALUES (:id, 'testco', 'active', 1)
            """
        ), {"id": TENANT_ID})
        # Insert valid API key
        conn.execute(text(
            """
            INSERT OR IGNORE INTO api_keys
                (id, tenant_id, key_hash, key_prefix, name, scopes, created_at)
            VALUES (:id, :tenant_id, :key_hash, 'gdx_live_tes', 'Test Key',
                    :scopes, :created_at)
            """
        ), {
            "id": API_KEY_ID,
            "tenant_id": TENANT_ID,
            "key_hash": API_KEY_HASH,
            "scopes": json.dumps(["read:jobs", "write:jobs", "read:customers", "write:customers"]),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return engine


def _make_tenant_engine():
    engine = create_engine(
        TENANT_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                lifecycle_stage TEXT NOT NULL DEFAULT 'lead',
                customer_id TEXT,
                scheduled_at TEXT,
                company_id TEXT,
                created_at TEXT,
                deleted_at TEXT
            )
            """
        ))
        # S122-9 slice 3: aligned to Customer ORM model so the new
        # ORM-based list/create/get endpoints don't fail on missing
        # columns. All hash/opt-out/cached columns are nullable.
        conn.execute(text(
            """
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
                qb_dirty INTEGER DEFAULT 0,
                qb_synced_at TEXT,
                updated_at TEXT,
                company_id TEXT,
                created_at TEXT,
                deleted_at TEXT
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                invoice_number TEXT,
                total REAL DEFAULT 0,
                status TEXT DEFAULT 'draft',
                company_id TEXT,
                created_at TEXT,
                deleted_at TEXT
            )
            """
        ))
        # Match WebhookEndpoint ORM model post-S122-9 slice 2. Pre-S122-9
        # fixture had `active` instead of `is_active` — vestigial from the
        # raw-SQL writer at `public_router.py:493` that was refactored to ORM.
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS webhook_endpoints (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                events TEXT DEFAULT '[]',
                secret TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        ))
    return engine


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

_FAKE_TENANT = {
    "id": TENANT_ID,
    "slug": "testco",
    "db_url": TENANT_DB_URL,
    "subscription_status": "active",
    "db_provisioned": 1,
}


@pytest.fixture(scope="module")
def client():
    """TestClient with full middleware isolation: in-memory DBs, no Redis."""
    import unittest.mock as _mock

    control_engine = _make_control_engine()
    tenant_engine = _make_tenant_engine()

    ControlSession = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)
    TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)

    # --- Patch TenantMiddleware _lookup_tenant before app creation ---
    import gdx_dispatch.core.tenant as _tenant_mod
    _orig_lookup = _tenant_mod._lookup_tenant

    def _patched_lookup(db, slug, tenant_id):
        if tenant_id == TENANT_ID or slug == "testco":
            return _FAKE_TENANT
        # No real DB call — return None for unknown tenants
        return None

    _tenant_mod._lookup_tenant = _patched_lookup

    # --- Single-tenant collapse (Phase A): TenantMiddleware now pins
    #     single_tenant() rather than resolving the x-tenant-id header.
    #     Pin it to THIS test's tenant so public_router's cross-tenant guard
    #     (request.state.tenant id vs the API key's tenant) matches. ---
    # Capture the patcher so teardown can stop it deterministically. Relying on
    # the process-global patch.stopall() alone is fragile across module-scoped
    # fixtures — a missed restore here leaks this tenant into single_tenant() for
    # every later test in the shard (it broke test_public_landing_leads' keys).
    _single_tenant_patcher = _mock.patch(
        "gdx_dispatch.core.tenant.single_tenant",
        new=lambda: {
            "id": TENANT_ID,
            "slug": "testco",
            "name": "Test Co",
            "db_url": TENANT_DB_URL,
            "subscription_status": "active",
            "db_provisioned": True,
        },
    )
    _single_tenant_patcher.start()

    # --- Patch SessionLocal in gdx_dispatch.core.database so TenantMiddleware
    #     and APIKeyMiddleware use the in-memory control DB ---
    import gdx_dispatch.core.database as _db_mod
    _orig_csf = _db_mod.SessionLocal
    _db_mod.SessionLocal = ControlSession  # type: ignore[assignment]

    # Also patch api_keys module if it cached SessionLocal at import time
    try:
        import gdx_dispatch.core.api_keys as _ak_mod
        _orig_ak_csf = _ak_mod.SessionLocal
        _ak_mod.SessionLocal = ControlSession  # type: ignore[assignment]
    except Exception:
        _orig_ak_csf = None
        _ak_mod = None  # type: ignore[assignment]

    # --- Stub Redis rate limiter: patch both check and get_remaining ---
    async def _noop_check(*args, **kwargs):  # noqa: RUF029
        return True  # Redis unavailable in unit tests — fail open

    async def _noop_get_remaining(*args, **kwargs):  # noqa: RUF029  # must match real async sig
        return 999  # Unused capacity

    _mock.patch("gdx_dispatch.core.rate_limiter.RateLimiter.check", new=_noop_check).start()
    _mock.patch("gdx_dispatch.core.rate_limiter.RateLimiter.get_remaining", new=_noop_get_remaining).start()

    # Build a minimal isolated FastAPI app using only public_router.
    # Avoids stale module-cache issues from gdx_dispatch.app being imported at collection time.
    import importlib
    import sys

    # Force fresh import of public_router so annotations are evaluated at current state
    for mod_name in [m for m in sys.modules if m in ("gdx_dispatch.api.public_router", "gdx_dispatch.api")]:
            del sys.modules[mod_name]

    from fastapi import FastAPI

    from gdx_dispatch.core.tenant import TenantMiddleware

    fresh_router_mod = importlib.import_module("gdx_dispatch.api.public_router")
    fresh_router = fresh_router_mod.router

    app = FastAPI()
    app.add_middleware(TenantMiddleware, control_session_factory=ControlSession)
    app.include_router(fresh_router)

    # --- FastAPI dependency overrides ---
    def _override_control_db():
        db = ControlSession()
        try:
            yield db
        finally:
            db.close()

    def _override_tenant_db():
        db = TenantSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[_db_mod.get_db] = _override_control_db
    app.dependency_overrides[_db_mod.get_db] = _override_tenant_db

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    # Restore patched state
    _tenant_mod._lookup_tenant = _orig_lookup
    _db_mod.SessionLocal = _orig_csf  # type: ignore[assignment]
    if _ak_mod is not None and _orig_ak_csf is not None:
        _ak_mod.SessionLocal = _orig_ak_csf  # type: ignore[assignment]
    _single_tenant_patcher.stop()
    _mock.patch.stopall()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPublicAPIAuth:
    def test_public_api_requires_key(self, client: TestClient):
        """GET /api/v1/jobs without X-API-Key must return 401."""
        resp = client.get("/api/v1/jobs", headers={"x-tenant-id": TENANT_ID})
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body

    def test_invalid_api_key_rejected(self, client: TestClient):
        """GET /api/v1/jobs with an unknown API key must return 401."""
        resp = client.get(
            "/api/v1/jobs",
            headers={
                "X-API-Key": "gdx_live_totally_invalid_key_not_in_db",
                "x-tenant-id": TENANT_ID,
            },
        )
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body


class TestPublicJobsAPI:
    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    def test_list_jobs_via_api(self, client: TestClient):
        """GET /api/v1/jobs with valid key returns paginated envelope."""
        resp = client.get("/api/v1/jobs", headers=self._headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:400]}"
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        meta = body["meta"]
        assert "page" in meta
        assert "per_page" in meta
        assert "total" in meta
        assert isinstance(body["data"], list)

    def test_create_job_via_api(self, client: TestClient):
        """POST /api/v1/jobs creates a job and returns 201 with id."""
        resp = client.post(
            "/api/v1/jobs",
            headers=self._headers,
            json={"title": "Test Job from API", "status": "lead"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "data" in body
        assert "id" in body["data"]
        assert body["data"]["title"] == "Test Job from API"


class TestPublicCustomersAPI:
    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    def test_list_customers_via_api(self, client: TestClient):
        """GET /api/v1/customers with valid key returns paginated envelope."""
        resp = client.get("/api/v1/customers", headers=self._headers)
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text[:400]}"
        body = resp.json()
        assert "data" in body
        assert "meta" in body
        assert isinstance(body["data"], list)
        meta = body["meta"]
        assert meta["page"] == 1
        assert meta["per_page"] == 20


class TestPublicWebhooksAPI:
    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    def test_webhook_registration(self, client: TestClient):
        """POST /api/v1/webhooks registers a webhook endpoint and returns 201."""
        resp = client.post(
            "/api/v1/webhooks",
            headers=self._headers,
            json={
                "url": "https://example.com/hooks/gdx",
                "events": ["job.created", "job.updated"],
                "secret": "mysecret123",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "id" in data
        assert data["url"] == "https://example.com/hooks/gdx"
        assert "job.created" in data["events"]
        assert data["active"] is True
