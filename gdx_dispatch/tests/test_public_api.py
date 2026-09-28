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
            "scopes": json.dumps(["read:jobs", "write:jobs", "read:customers", "write:customers", "listings:read"]),
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
                deleted_at TEXT,
                -- 2026-07-30: the real table has carried job_type since the
                -- beginning; the taxonomy work's INSERT now names it, and a
                -- fixture claiming to mirror the ORM must carry it too.
                job_type TEXT
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
                -- Added with migration 070. This file hand-rolls its own
                -- customers DDL, so every ORM column has to be mirrored here
                -- by hand or the SELECT 500s. (One more entry for the
                -- create_app()-fixture debt in the CI/test assessment.)
                local_edit_at TEXT,
                local_edit_fields TEXT,
                customer_type TEXT,
                can_submit_listings INTEGER DEFAULT 0,
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
        c._tenant_engine = tenant_engine  # type: ignore[attr-defined]
        yield c

    # Restore patched state
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
        assert resp.status_code == 201, f"got {resp.status_code}: {resp.text[:500]}"
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


class TestPublicListingsAPI:
    """The door-listings feed garagedoorxperts.com renders.

    The risk on this route is not availability, it's exposure: an unapproved
    door — a tech's rough draft, or a customer's submission the office hasn't
    ruled on — must be unreachable even with a perfectly valid API key.
    """

    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    def test_requires_an_api_key(self, client: TestClient):
        assert client.get("/api/v1/listings").status_code == 401

    def test_returns_only_published_doors(self, client: TestClient):
        from sqlalchemy.orm import sessionmaker

        from gdx_dispatch.modules.door_listings.models import DoorListing, DoorListingPhoto

        # This fixture's tenant DB is built from hand-written DDL, so create the
        # ORM-managed listing tables the same way create_orm_tables() does at boot.
        DoorListing.__table__.create(bind=client._tenant_engine, checkfirst=True)
        DoorListingPhoto.__table__.create(bind=client._tenant_engine, checkfirst=True)
        s = sessionmaker(bind=client._tenant_engine)()
        s.add(DoorListing(title="live door", slug="live-door", status="published",
                          listing_type="used", source="office"))
        s.add(DoorListing(title="draft door", slug="draft-door", status="draft",
                          listing_type="used", source="office"))
        s.add(DoorListing(title="pending door", slug="pending-door", status="pending_review",
                          listing_type="used", source="tech"))
        s.add(DoorListing(title="sold door", slug="sold-door", status="sold",
                          listing_type="used", source="office"))
        s.commit()
        s.close()

        resp = client.get("/api/v1/listings", headers=self._headers)
        assert resp.status_code == 200, resp.text[:2000]
        titles = [r["title"] for r in resp.json()["data"]]
        assert titles == ["live door"], titles


class TestDbErrorsLeaveATrace:
    """Silent-500 regression net (2026-09-20).

    Until this date the nine public-API DB-error handlers caught
    ``Exception as exc`` and raised an opaque 500 ``from None`` with NO log
    call — on a prod box with no Sentry, a failing external integration was
    invisible and undebuggable. Pin, per handler, that a DB failure now
    (a) still answers the opaque 500 (no internals leak to API-key callers)
    and (b) leaves a ``log.exception`` record naming the handler.

    Covers 8 of the 9 handlers. The ninth, create_public_landing_lead, has
    the identical log-then-raise shape (hand-verified) but sits behind
    scope/turnstile gates this file's API key doesn't carry; its own flows
    live in test_public_landing_leads.py.
    """

    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    _JOB_ID = "00000000-0000-0000-0000-000000000001"

    @pytest.mark.parametrize(
        ("seam", "method", "path", "payload", "handler"),
        [
            ("text", "GET", "/api/v1/jobs", None, "list_jobs"),
            ("text", "GET", f"/api/v1/jobs/{_JOB_ID}", None, "get_job"),
            ("text", "POST", "/api/v1/jobs", {"title": "boom probe"}, "create_job"),
            ("text", "PATCH", f"/api/v1/jobs/{_JOB_ID}", {"title": "boom probe"}, "update_job"),
            # the customers pair queries via the ORM with function-local
            # imports, out of reach of the module seams — hand them a session
            # whose every attribute access raises, via the get_db override.
            ("db", "GET", "/api/v1/customers", None, "list_customers"),
            ("db", "POST", "/api/v1/customers", {"name": "boom probe"}, "create_customer"),
            ("text", "GET", "/api/v1/invoices", None, "list_invoices"),
            # register_webhook writes via the ORM, not text(); break the model
            # instead — db.add(<unmapped object>) raises inside its try.
            ("webhook_model", "POST", "/api/v1/webhooks", {"url": "https://example.com/hook"}, "register_webhook"),
        ],
    )
    def test_a_db_error_logs_and_answers_an_opaque_500(
        self, client: TestClient, monkeypatch, caplog, seam, method, path, payload, handler
    ):
        import logging as _logging

        from gdx_dispatch.api import public_router as pr

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated database failure")

        restore_override = None
        if seam == "text":
            # Every raw-SQL statement goes through the module's `text(...)`,
            # so this fails inside the handler's try — the same place a real
            # DB error (bad column, PG outage) lands.
            monkeypatch.setattr(pr, "text", _boom)
        elif seam == "webhook_model":
            monkeypatch.setattr(pr, "WebhookEndpoint", lambda **kw: object())
        else:  # seam == "db"
            class _BrokenDB:
                def __getattr__(self, name):
                    raise RuntimeError("simulated database failure")

            def _broken_db():
                yield _BrokenDB()

            app = client.app
            restore_override = (app, app.dependency_overrides.get(pr.get_db))
            app.dependency_overrides[pr.get_db] = _broken_db

        try:
            with caplog.at_level(_logging.ERROR, logger="gdx_dispatch.api.public_router"):
                resp = client.request(method, path, headers=self._headers, json=payload)
        finally:
            if restore_override is not None:
                app, prev = restore_override
                if prev is None:
                    app.dependency_overrides.pop(pr.get_db, None)
                else:
                    app.dependency_overrides[pr.get_db] = prev
        assert resp.status_code == 500, f"{handler}: expected 500, got {resp.status_code}: {resp.text[:300]}"
        assert resp.json()["detail"] == "A database error occurred"
        assert "simulated database failure" not in resp.text  # opaque to callers
        matching = [r for r in caplog.records if f"public api {handler} failed" in r.getMessage()]
        assert matching, f"{handler}: no log record; records: {[r.getMessage() for r in caplog.records]!r}"
        assert matching[0].exc_info is not None  # full traceback captured


# ---------------------------------------------------------------------------
# Invariant #1 — every public-API mutation leaves a trail (GDXA-85)
# ---------------------------------------------------------------------------


def _audit_rows(client: TestClient, *, action: str | None = None,
                entity_id: str | None = None) -> list[dict]:
    """Every audit row in the tenant DB, newest last, details decoded.

    Read straight out of the fixture engine rather than through the router:
    the point is what *landed*, and `get_db()` closes without committing, so a
    row only shows up here if the handler's audit path really committed it
    (bug class #700).
    """
    with client._tenant_engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(text(
            "SELECT action, entity_type, entity_id, user_id, tenant_id, details, "
            "       ip_address, row_hash, prev_hash "
            "  FROM audit_logs ORDER BY created_at, id"
        )).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("details")
        d["details"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
        out.append(d)
    if action is not None:
        out = [r for r in out if r["action"] == action]
    if entity_id is not None:
        out = [r for r in out if r["entity_id"] == entity_id]
    return out


class TestPublicApiMutationsAreAudited:
    """GDXA-85: four mutation routes committed with no audit row at all.

    Invariant #1 says every create/update/delete answers who did it, what
    changed and when. On a public-API request there is no human to name, so
    "who" is the API key: these rows carry the key prefix as the actor and
    `details.channel == "public_api"`, which is what separates them from the
    in-app rows sharing the same action name.

    What would make these tests worthless: asserting the handler *calls* an
    audit helper (a mock proves which arguments went in, never that a row came
    out). Every assertion below reads the committed row back out of the
    database instead.
    """

    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    def test_job_create_is_audited(self, client: TestClient):
        resp = client.post(
            "/api/v1/jobs",
            headers=self._headers,
            json={"title": "Audited job", "status": "lead"},
        )
        assert resp.status_code == 201, resp.text[:400]
        job_id = resp.json()["data"]["id"]

        rows = _audit_rows(client, action="job_created", entity_id=job_id)
        assert len(rows) == 1, f"expected exactly one trail row, got {rows!r}"
        row = rows[0]
        assert row["entity_type"] == "job"
        assert row["tenant_id"] == TENANT_ID
        assert row["user_id"] == "gdx_live_tes"  # the key prefix, not "system"
        assert row["details"]["channel"] == "public_api"
        assert row["details"]["api_key_prefix"] == "gdx_live_tes"
        assert row["details"]["title"] == "Audited job"
        assert row["row_hash"]  # hash chain populated

    def test_job_update_is_audited_with_the_columns_it_wrote(self, client: TestClient):
        created = client.post(
            "/api/v1/jobs", headers=self._headers, json={"title": "before"}
        )
        job_id = created.json()["data"]["id"]

        resp = client.patch(
            f"/api/v1/jobs/{job_id}",
            headers=self._headers,
            json={
                "title": "after",
                "status": "scheduled",
                # A datetime in `details` is the shape that broke a prod audit
                # insert (core/audit.py: an expense PATCH's raw `date` raised
                # StatementError AFTER the mutation committed). Here a refused
                # row is swallowed by design, so it would vanish silently —
                # this field is why the assertion below is on the stored row.
                "scheduled_at": "2026-10-01T15:30:00+00:00",
            },
        )
        assert resp.status_code == 200, resp.text[:400]

        rows = _audit_rows(client, action="job_updated", entity_id=job_id)
        assert len(rows) == 1, f"expected exactly one trail row, got {rows!r}"
        changed = rows[0]["details"]["changed"]
        # The columns, not the request body: `status` lands in lifecycle_stage.
        assert set(changed) == {"title", "lifecycle_stage", "scheduled_at"}, changed
        assert changed["title"] == "after"
        assert changed["lifecycle_stage"] == "scheduled"
        assert changed["scheduled_at"].startswith("2026-10-01"), changed["scheduled_at"]
        assert rows[0]["user_id"] == "gdx_live_tes"

    def test_an_update_that_matched_nothing_writes_no_row(self, client: TestClient):
        """A 404 changed nothing, so a row for it would be a false entry.

        A real update runs first so the assertion cannot pass merely because
        nothing in this module has written an audit row yet — run this test
        alone and it still distinguishes the two cases.
        """
        created = client.post(
            "/api/v1/jobs", headers=self._headers, json={"title": "real target"}
        )
        real_id = created.json()["data"]["id"]
        assert client.patch(
            f"/api/v1/jobs/{real_id}", headers=self._headers, json={"title": "touched"}
        ).status_code == 200
        assert len(_audit_rows(client, action="job_updated", entity_id=real_id)) == 1

        missing = "00000000-0000-0000-0000-0000000000ff"
        resp = client.patch(
            f"/api/v1/jobs/{missing}", headers=self._headers, json={"title": "ghost"}
        )
        assert resp.status_code == 404, resp.text[:300]
        assert _audit_rows(client, action="job_updated", entity_id=missing) == []

    def test_customer_create_is_audited_without_leaking_contact_details(
        self, client: TestClient
    ):
        resp = client.post(
            "/api/v1/customers",
            headers=self._headers,
            json={
                "name": "Audited Customer",
                "email": "leak@example.com",
                "phone": "612-555-0001",
                "address": "1 Secret Lane",
            },
        )
        assert resp.status_code == 201, resp.text[:400]
        customer_id = resp.json()["data"]["id"]

        rows = _audit_rows(client, action="customer_created", entity_id=customer_id)
        assert len(rows) == 1, f"expected exactly one trail row, got {rows!r}"
        details = rows[0]["details"]
        assert details["name"] == "Audited Customer"
        assert rows[0]["entity_type"] == "customer"
        assert rows[0]["user_id"] == "gdx_live_tes"
        # core.audit does not redact `details`; it only JSON-normalizes it. The
        # contact columns (address is EncryptedString at rest) must not be
        # copied into a plaintext audit payload.
        blob = json.dumps(details)
        assert "leak@example.com" not in blob
        assert "612-555-0001" not in blob
        assert "1 Secret Lane" not in blob

    def test_webhook_registration_is_audited_and_never_stores_the_secret(
        self, client: TestClient
    ):
        resp = client.post(
            "/api/v1/webhooks",
            headers=self._headers,
            json={
                "url": "https://example.com/hooks/audited",
                "events": ["job.created"],
                "secret": "sup3r-secret-value",
            },
        )
        assert resp.status_code == 201, resp.text[:400]
        endpoint_id = resp.json()["data"]["id"]

        rows = _audit_rows(
            client, action="webhook_endpoint_registered", entity_id=endpoint_id
        )
        assert len(rows) == 1, f"expected exactly one trail row, got {rows!r}"
        details = rows[0]["details"]
        assert rows[0]["entity_type"] == "webhook_endpoint"
        assert rows[0]["user_id"] == "gdx_live_tes"
        assert details["url"] == "https://example.com/hooks/audited"
        assert details["events"] == ["job.created"]
        assert details["secret_set"] is True
        # The secret is a credential; `details` is readable in the audit viewer.
        assert "sup3r-secret-value" not in json.dumps(details)


class TestAuditFailureSemantics:
    """What happens when the audit table itself refuses the row.

    Every mutation route here stages its trail row inside its own transaction,
    so the answer is the same for all of them: the change does not stand. That
    is the repo's canonical shape — auditing after the commit is for a change
    committed by a helper the handler cannot reach into, and these handlers each
    own their single commit.

    The refusal comes from the storage layer (a real BEFORE INSERT trigger), not
    a patched function: the behaviour under test is what a failed *flush* does
    to the session, and only a real statement failure produces that.
    """

    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    @staticmethod
    def _refuse_audit_inserts(client: TestClient):
        """Install the refusal; caller must drop it. audit_logs is created
        lazily by ensure_audit_table, so make sure it exists first."""
        engine = client._tenant_engine  # type: ignore[attr-defined]
        with engine.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT,
                    action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
                    details JSON, ip_address TEXT, request_id TEXT,
                    row_hash TEXT NOT NULL, prev_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT, actor_id TEXT, actor_role TEXT, payload JSON, hash TEXT
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TRIGGER audit_logs_refuse_insert
                BEFORE INSERT ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'audit storage refuses this row');
                END;
                """
            ))
        return engine

    def test_a_refused_row_leaves_no_unaudited_job_and_no_duplicate(
        self, client: TestClient, caplog
    ):
        """No job without a trail — and the honest 500 costs nothing, because
        the write was never committed, so a retry cannot duplicate it.

        An audit-after-commit shape would answer 201 here and silently lose the
        row. That was this fix's first draft, and the reason it changed.
        """
        import logging as _logging

        title = "refused trail"
        engine = self._refuse_audit_inserts(client)
        try:
            with caplog.at_level(_logging.ERROR, logger="gdx_dispatch.core.audit"):
                first = client.post("/api/v1/jobs", headers=self._headers, json={"title": title})
                second = client.post("/api/v1/jobs", headers=self._headers, json={"title": title})
        finally:
            with engine.begin() as conn:
                conn.execute(text("DROP TRIGGER IF EXISTS audit_logs_refuse_insert"))

        for resp in (first, second):
            assert resp.status_code == 500, resp.text[:400]
            assert resp.json()["detail"] == "audit failure — change rolled back"
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM jobs WHERE title = :t"), {"t": title}
            ).scalar()
        assert count == 0, "an unaudited job was left behind"
        assert any(
            "audit_write_failed action=job_created" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_refused_row_leaves_no_unaudited_customer(self, client: TestClient):
        """The same for the ORM-routed write, which reaches its id via flush()."""
        name = "refused trail co"
        engine = self._refuse_audit_inserts(client)
        try:
            resp = client.post("/api/v1/customers", headers=self._headers, json={"name": name})
        finally:
            with engine.begin() as conn:
                conn.execute(text("DROP TRIGGER IF EXISTS audit_logs_refuse_insert"))

        assert resp.status_code == 500, resp.text[:400]
        assert resp.json()["detail"] == "audit failure — change rolled back"
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM customers WHERE name = :n"), {"n": name}
            ).scalar()
        assert count == 0, "an unaudited customer was left behind"

    def test_a_refused_row_rolls_the_webhook_registration_back(self, client: TestClient):
        """Registering an endpoint grants a data-egress channel; an untraceable
        one must not exist."""
        url = "https://example.com/hooks/must-not-persist"
        engine = self._refuse_audit_inserts(client)
        try:
            resp = client.post(
                "/api/v1/webhooks",
                headers=self._headers,
                json={"url": url, "events": ["job.created"], "secret": "x"},
            )
        finally:
            with engine.begin() as conn:
                conn.execute(text("DROP TRIGGER IF EXISTS audit_logs_refuse_insert"))

        assert resp.status_code == 500, resp.text[:400]
        # The audit-specific message, not the generic database one: the caller
        # is told the change was undone, which is what actually happened.
        assert resp.json()["detail"] == "audit failure — change rolled back"
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM webhook_endpoints WHERE url = :u"), {"u": url}
            ).scalar()
        assert count == 0, "an unaudited egress endpoint was left registered"


# ---------------------------------------------------------------------------
# GDXA-145 — a write that landed is never answered as a failure
# ---------------------------------------------------------------------------


class TestADurableWriteIsNotReportedAsAFailure:
    """`_write_errors_as_500` turns anything raised in its body into the opaque
    500 and calls `db.rollback()`. Past the commit that rollback is a no-op, so
    a statement left inside the block after the commit hands an API consumer
    "A database error occurred" for a row that is already on disk.

    That is an audit problem, not a status-code nit: the consumer retries, and
    the retry creates a second customer / a second egress endpoint, each with
    its own `*_created` trail row. Two rows describing one intended change is a
    false trail, which is why this sits with the invariant #1 work.

    The narrowing, stated honestly: this closes the window between the COMMIT
    and the response, not the retry class. There is no idempotency key and no
    unique constraint behind these routes — two identical `POST /api/v1/webhooks`
    calls give two endpoints for one URL, measured — so a connection lost
    *during* the COMMIT is still an ambiguous write answered as 500, and any 500
    a consumer retries still duplicates. What is fixed is the case where nothing
    was ambiguous at all.

    The seam is `dead_after_commit` (conftest): the engine refuses every
    statement issued after its first commit inside the block, which is what a
    pgbouncer restart or a PG failover does in that window. Both assertions in
    each test below fail on the pre-GDXA-145 code — measured: 500, with one
    refused statement, the `db.refresh()` that used to sit after the commit.

    All five write handlers get a probe, not just the three this fix changed.
    `create_job` / `update_job` were already clean — they build their response
    from a RETURNING mapping — but the AST guard below is lexical and stops at a
    call boundary (`return _ok(_job_body(db, job_id))` passes it), so a probe per
    route is the only net that does not depend on where the read is written.
    """

    _headers = {"X-API-Key": RAW_API_KEY, "x-tenant-id": TENANT_ID}

    def test_a_committed_job_is_reported_as_created_and_updated(
        self, client: TestClient, dead_after_commit
    ):
        """The two handlers this fix did not have to change, pinned anyway.

        Both reach their response through a RETURNING mapping captured before the
        commit, so `refused` must be 0 for each. Without this, the only thing
        keeping them clean is a lexical guard that a one-line refactor into a
        helper walks straight through.
        """
        engine = client._tenant_engine  # type: ignore[attr-defined]
        assert client.post(
            "/api/v1/jobs", headers=self._headers, json={"title": "warm up"}
        ).status_code == 201

        with dead_after_commit(engine) as seam:
            created = client.post(
                "/api/v1/jobs", headers=self._headers, json={"title": "postcommit job"}
            )
        assert created.status_code == 201, (
            f"a durable write answered {created.status_code} after "
            f"{seam['refused']} post-commit statement(s): {created.text[:300]}"
        )
        assert seam["refused"] == 0, "create_job touched the DB after committing"
        job_id = created.json()["data"]["id"]
        assert created.json()["data"]["title"] == "postcommit job"

        with dead_after_commit(engine) as seam:
            patched = client.patch(
                f"/api/v1/jobs/{job_id}", headers=self._headers, json={"title": "after"}
            )
        assert patched.status_code == 200, (
            f"a durable update answered {patched.status_code} after "
            f"{seam['refused']} post-commit statement(s): {patched.text[:300]}"
        )
        assert seam["refused"] == 0, "update_job touched the DB after committing"
        assert patched.json()["data"]["title"] == "after"

    def test_a_committed_customer_is_reported_as_created(
        self, client: TestClient, dead_after_commit
    ):
        engine = client._tenant_engine  # type: ignore[attr-defined]
        # Warm the audit table first: `ensure_audit_table` commits on its first
        # run per engine, and that commit would arm the seam before the INSERT
        # under test — the probe would then measure the wrong window.
        assert client.post(
            "/api/v1/customers", headers=self._headers, json={"name": "warm up"}
        ).status_code == 201

        name = "postcommit probe"
        with dead_after_commit(engine) as seam:
            resp = client.post(
                "/api/v1/customers",
                headers=self._headers,
                json={"name": name, "email": "probe@example.com"},
            )

        assert resp.status_code == 201, (
            f"a durable write answered {resp.status_code}; the handler issued "
            f"{seam['refused']} statement(s) after its commit: {resp.text[:300]}"
        )
        assert seam["refused"] == 0, (
            f"{seam['refused']} statement(s) ran after the commit — each one is a "
            "way to fail a write that already landed"
        )
        data = resp.json()["data"]
        assert data["name"] == name
        assert data["email"] == "probe@example.com"
        # Populated by the flush (default=utcnow), not by a re-read: a handler
        # that needed a post-commit SELECT for this field could not answer here.
        assert data["created_at"], data

        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM customers WHERE name = :n"), {"n": name}
            ).scalar()
        assert count == 1, "the write the 201 promised is not in the table"
        rows = _audit_rows(client, action="customer_created", entity_id=data["id"])
        assert len(rows) == 1, f"expected one trail row for the create, got {rows!r}"

    def test_a_committed_webhook_endpoint_is_reported_as_registered(
        self, client: TestClient, dead_after_commit
    ):
        """The retry case that matters most: a duplicate egress endpoint.

        Answering 500 for a registration that landed leaves the key holder
        retrying, and two endpoints pointed at the same URL each deliver every
        event — with two `webhook_endpoint_registered` rows for one intended
        grant.
        """
        engine = client._tenant_engine  # type: ignore[attr-defined]
        url = "https://example.com/hooks/postcommit-probe"
        assert client.post(
            "/api/v1/webhooks",
            headers=self._headers,
            json={"url": "https://example.com/hooks/warm-up", "secret": "x"},
        ).status_code == 201

        with dead_after_commit(engine) as seam:
            resp = client.post(
                "/api/v1/webhooks",
                headers=self._headers,
                json={"url": url, "events": ["job.created"], "secret": "s3cret"},
            )

        assert resp.status_code == 201, (
            f"a durable write answered {resp.status_code}; the handler issued "
            f"{seam['refused']} statement(s) after its commit: {resp.text[:300]}"
        )
        assert seam["refused"] == 0, (
            f"{seam['refused']} statement(s) ran after the commit — each one is a "
            "way to fail a registration that already landed"
        )
        data = resp.json()["data"]
        assert data["url"] == url
        assert data["events"] == ["job.created"]
        assert data["active"] is True
        assert data["created_at"], data

        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM webhook_endpoints WHERE url = :u"), {"u": url}
            ).scalar()
        assert count == 1, "the registration the 201 promised is not in the table"
        rows = _audit_rows(
            client, action="webhook_endpoint_registered", entity_id=data["id"]
        )
        assert len(rows) == 1, f"expected one trail row for the grant, got {rows!r}"

    def test_every_write_block_ends_at_its_commit(self):
        """The contract `_write_errors_as_500`'s docstring states, enforced.

        Reads the module's AST, not its text, and checks two things per handler:

        1. the last statement in the `with _write_errors_as_500(...)` body is
           `db.commit()` — anything after it is inside the translation, so it
           answers the opaque 500 for a write that landed;
        2. no statement *after* the block, written in the handler itself, calls a
           method on `db` or reads an attribute off the ORM instance it just
           wrote — unless it sits in a `try` whose handlers raise nothing, or in
           a `contextlib.suppress(...)`. Moving the same `db.refresh()` one line
           down, out of the block, is not a fix: with `expire_on_commit` at its
           default True that read is a SELECT, and past the `with` it answers a
           bare `Internal Server Error` instead. Rule 1 alone passed that
           variant — measured, which is why rule 2 exists.

        Rule 3 then closes the way around both: every write route in this module
        (`@router.post/patch/put/delete`) must open a `_write_errors_as_500`
        block at all. Without it a sixth handler that simply never adopts the
        wrapper is invisible here — measured: appending one made no rule fire.

        What this guard does NOT see, said plainly:

        * It is lexical and stops at the call boundary. Hand the instance or the
          session to a helper (`return _ok(_customer_body(customer))`) and rule 2
          stays green while the 500 is live — measured. That is why every one of
          the five handlers also has a runtime probe above; this guard is the
          cheap net that catches the obvious regression in place, not the proof.
        * It checks position, never whether a statement *can* fail.
        * It is scoped to this one file. The same shape — a DB statement after
          `db.commit()` inside a block that answers 5xx — exists at 21 other
          call sites in 13 files owned by other domains (counted in the PR body,
          `routers/customers.py:490` the worst of them: its audit write sits
          *after* the try, so a post-commit failure there leaves a committed
          customer with no trail row at all). Guarding the class repo-wide
          belongs in `tools/audit_after_commit_scan.py`, which already has the
          AST machinery and an allowance ledger; that is a separate change.
        """
        import ast
        import pathlib

        src = pathlib.Path(pr_module_path()).read_text()
        tree = ast.parse(src)

        def blocks_in(fn):
            return [
                node
                for node in ast.walk(fn)
                if isinstance(node, ast.With)
                and any(
                    isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "id", None)
                    == "_write_errors_as_500"
                    for item in node.items
                )
            ]

        functions = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and blocks_in(n)
        ]
        total = sum(len(blocks_in(fn)) for fn in functions)
        assert total == 5, (
            f"expected the five public-API write handlers, found {total} "
            "`_write_errors_as_500` blocks — update this guard with the new one"
        )

        # --- rule 3: every write route opens the block in the first place ---
        # Rules 1 and 2 only look inside `_write_errors_as_500` blocks, so a new
        # handler that never adopts the wrapper is invisible to them. Counting the
        # routes instead of the blocks is what makes "any handler added later"
        # true rather than aspirational.
        write_routes = [
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(d, ast.Call)
                and getattr(d.func, "attr", None) in ("post", "patch", "put", "delete")
                and getattr(getattr(d.func, "value", None), "id", None) == "router"
                for d in fn.decorator_list
            )
        ]
        assert write_routes, "found no @router.post/patch/put/delete at all — guard broken"
        unwrapped = [fn.name for fn in write_routes if not blocks_in(fn)]
        assert not unwrapped, (
            f"write route(s) {unwrapped} run outside `_write_errors_as_500`: their "
            "DB failures do not become the opaque 500, and rules 1 and 2 cannot see "
            "a post-commit statement in them at all"
        )

        for fn in functions:
            for block in blocks_in(fn):
                # --- rule 1: the commit is the last statement in the block ---
                last = block.body[-1]
                call = last.value if isinstance(last, ast.Expr) else None
                assert (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "commit"
                    # The receiver too: `something_else.commit()` is not the
                    # handler's own commit, and accepting it would let the block
                    # end on a statement that commits nothing.
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "db"
                ), (
                    f"{fn.name}: the `_write_errors_as_500` block at line "
                    f"{block.lineno} ends with {ast.dump(last)[:120]}, not "
                    "db.commit() — a statement after the commit inside this block "
                    "answers 500 for a durable write"
                )

                # ...and at every depth, not just the top level of the body.
                # `if payload.phone: db.commit(); db.refresh(customer)` leaves the
                # block ending on a commit while a post-commit read hides on a
                # branch no probe's payload takes — measured: all other rules
                # passed. `update_job` already nests an `if row:` inside its own
                # block, so this shape is native to the file, not hypothetical.
                commits = [
                    n
                    for n in ast.walk(block)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "commit"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "db"
                ]
                first_commit = min(c.lineno for c in commits)
                for n in ast.walk(block):
                    if (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "db"
                        and n.func.attr != "commit"
                        and n.lineno > first_commit
                    ):
                        raise AssertionError(
                            f"{fn.name}: db.{n.func.attr}() at line {n.lineno} runs "
                            f"after the commit at line {first_commit}, still inside the "
                            "block — it answers the opaque 500 for a durable write, on "
                            "whatever branch it sits"
                        )

                # --- rule 2: nothing reachable-as-an-error after the block ---
                # Names bound to an ORM instance in this handler: `x = Model(...)`
                # or `x = models.Model(...)` — the callee's last component being
                # Capitalized is what marks it, so a qualified construction is
                # caught too.
                def _model_call(value):
                    if not isinstance(value, ast.Call):
                        return False
                    fnode = value.func
                    name = getattr(fnode, "id", None) or getattr(fnode, "attr", None)
                    return bool(name) and name[:1].isupper()

                orm_names = {
                    t.id
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Assign) and _model_call(n.value)
                    for t in n.targets
                    if isinstance(t, ast.Name)
                }
                # Line spans a failure cannot escape from:
                #   * the BODY of a `try` whose handlers raise nothing — the
                #     landing-lead handler's audit and notification blocks, by
                #     design;
                #   * a `with contextlib.suppress(...)` block, anywhere.
                # A handler's own `except` body is deliberately NOT such a span:
                # a bare `db.rollback()` there raises *out* of the block it was
                # meant to contain, which is how a committed lead answered a bare
                # "Internal Server Error" (measured; the fix wraps both in
                # suppress, and this is what keeps them wrapped).
                def _swallows(t):
                    """A try that really cannot let a failure out.

                    Three conditions, and the last two were wrong in the first
                    draft: a handler catching a narrower type lets everything else
                    through, and a bare `try/finally` has no handlers at all — so
                    `not any(...)` called it swallowing and a post-commit refresh
                    under `except ValueError:` sailed past rule 2 (measured).
                    """
                    if not t.body or not t.handlers:
                        return False
                    for h in t.handlers:
                        if any(isinstance(s, ast.Raise) for s in ast.walk(h)):
                            return False
                        caught = h.type
                        names = (
                            [getattr(e, "id", None) for e in caught.elts]
                            if isinstance(caught, ast.Tuple)
                            else [getattr(caught, "id", None)]
                        )
                        if not all(n in ("Exception", "BaseException") for n in names):
                            return False
                    return True

                swallowing = [
                    (t.body[0].lineno, t.body[-1].end_lineno)
                    for t in ast.walk(fn)
                    if isinstance(t, ast.Try) and _swallows(t)
                ] + [
                    (w.lineno, w.end_lineno)
                    for w in ast.walk(fn)
                    if isinstance(w, ast.With)
                    and any(
                        isinstance(i.context_expr, ast.Call)
                        and getattr(i.context_expr.func, "attr", None) == "suppress"
                        for i in w.items
                    )
                ]

                def contained(node, spans=swallowing):
                    return any(lo <= node.lineno <= hi for lo, hi in spans)

                for node in ast.walk(fn):
                    if getattr(node, "lineno", 0) <= block.end_lineno or contained(node):
                        continue
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "db"
                    ):
                        raise AssertionError(
                            f"{fn.name}: db.{node.func.attr}() at line {node.lineno} runs "
                            "after the write block and outside any swallowing try — it "
                            "can answer an error for a write that already landed"
                        )
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in orm_names
                    ):
                        raise AssertionError(
                            f"{fn.name}: reads {node.value.id}.{node.attr} at line "
                            f"{node.lineno}, after the write block — with "
                            "expire_on_commit=True that is a SELECT, and it answers an "
                            "error for a write that already landed. Snapshot it before "
                            "the commit instead."
                        )


def pr_module_path() -> str:
    """Where public_router.py lives, without importing it.

    The module itself is imported and deleted from `sys.modules` by the `client`
    fixture; resolving the path off the package directory keeps the AST guard
    independent of that.
    """
    import pathlib

    import gdx_dispatch

    return str(pathlib.Path(gdx_dispatch.__file__).parent / "api" / "public_router.py")
