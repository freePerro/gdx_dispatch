"""gdx_dispatch/tests/test_public_landing_leads.py — Public landing-lead intake endpoint.

Covers all 9 acceptance cases from sprint_public_lead_intake.md:
1. Valid key + scope + Turnstile (fail-open w/o secret) → 201 + row + audit
2. Missing X-API-Key → 401
3. Wrong / unknown key → 401
4. Revoked key → 401
5. Key without `landing_leads:write` scope → 403
6. Honeypot field filled → 201 silently, no DB insert
7. Turnstile fail (mocked) → 400
8. Cross-tenant: key for tenant A used on tenant B subdomain → resolves to tenant A
9. Audit row contains key prefix, origin, IP, turnstile/honeypot flags
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from gdx_dispatch.core.tenant import single_tenant

# ---------------------------------------------------------------------------
# Fixtures — mirror test_public_api.py shape
# ---------------------------------------------------------------------------

# Single-tenant collapse: TenantMiddleware pins every request to the one tenant
# (single_tenant() / GDX_TENANT_ID, set to the canonical GDX_UUID in conftest).
# The legitimate keys must belong to THAT tenant to clear public_router's
# retained cross-tenant guard. TENANT_FOREIGN_ID is a deliberately non-pinned id
# kept only to prove the guard still rejects a key whose tenant != the pinned one.
TENANT_A_ID = single_tenant()["id"]
TENANT_FOREIGN_ID = "99999999-9999-9999-9999-999999999999"

# Keys: A has landing_leads:write, B does not — both on the single pinned tenant.
# FOREIGN has the scope but belongs to a non-pinned tenant (guard-rejection test).
RAW_KEY_A = "gdx_live_landingleadsAAAAAAAAAAAAAAAA"
RAW_KEY_B_NOSCOPE = "gdx_live_landingleadsBBBBBBBBBBBBBBBB"
RAW_KEY_REVOKED = "gdx_live_landingleadsCCCCCCCCCCCCCCCC"
RAW_KEY_FOREIGN = "gdx_live_landingleadsFFFFFFFFFFFFFFFF"

KEY_A_HASH = hashlib.sha256(RAW_KEY_A.encode()).hexdigest()
KEY_B_HASH = hashlib.sha256(RAW_KEY_B_NOSCOPE.encode()).hexdigest()
KEY_REVOKED_HASH = hashlib.sha256(RAW_KEY_REVOKED.encode()).hexdigest()
KEY_FOREIGN_HASH = hashlib.sha256(RAW_KEY_FOREIGN.encode()).hexdigest()


def _make_control_engine() -> object:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

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
        # Insert tenants
        for tid, slug in [(TENANT_A_ID, "tenanta"), (TENANT_FOREIGN_ID, "foreign")]:
            conn.execute(text(
                """
                INSERT OR IGNORE INTO tenants (id, slug, subscription_status, db_provisioned)
                VALUES (:id, :slug, 'active', 1)
                """
            ), {"id": tid, "slug": slug})

        now_iso = datetime.now(timezone.utc).isoformat()
        # Key A: tenant A, has landing_leads:write
        conn.execute(text(
            """
            INSERT OR IGNORE INTO api_keys
                (id, tenant_id, key_hash, key_prefix, name, scopes, created_at)
            VALUES (:id, :tid, :h, 'gdx_live_landi', 'Marketing site',
                    :scopes, :ts)
            """
        ), {
            "id": str(uuid.uuid4()),
            "tid": TENANT_A_ID,
            "h": KEY_A_HASH,
            "scopes": json.dumps(["landing_leads:write"]),
            "ts": now_iso,
        })
        # Key B: single (pinned) tenant, NO landing_leads:write scope — so the
        # cross-tenant guard passes and the request reaches the scope check.
        conn.execute(text(
            """
            INSERT OR IGNORE INTO api_keys
                (id, tenant_id, key_hash, key_prefix, name, scopes, created_at)
            VALUES (:id, :tid, :h, 'gdx_live_lan_b', 'No-scope key',
                    :scopes, :ts)
            """
        ), {
            "id": str(uuid.uuid4()),
            "tid": TENANT_A_ID,
            "h": KEY_B_HASH,
            "scopes": json.dumps(["read:jobs"]),
            "ts": now_iso,
        })
        # Key FOREIGN: valid scope but bound to a NON-pinned tenant. Used only to
        # prove public_router's cross-tenant guard still rejects a key whose
        # tenant != the single pinned tenant.
        conn.execute(text(
            """
            INSERT OR IGNORE INTO api_keys
                (id, tenant_id, key_hash, key_prefix, name, scopes, created_at)
            VALUES (:id, :tid, :h, 'gdx_live_lan_f', 'Foreign-tenant key',
                    :scopes, :ts)
            """
        ), {
            "id": str(uuid.uuid4()),
            "tid": TENANT_FOREIGN_ID,
            "h": KEY_FOREIGN_HASH,
            "scopes": json.dumps(["landing_leads:write"]),
            "ts": now_iso,
        })
        # Revoked key (also for tenant A)
        conn.execute(text(
            """
            INSERT OR IGNORE INTO api_keys
                (id, tenant_id, key_hash, key_prefix, name, scopes, created_at, revoked_at)
            VALUES (:id, :tid, :h, 'gdx_live_lan_r', 'Revoked',
                    :scopes, :ts, :ts)
            """
        ), {
            "id": str(uuid.uuid4()),
            "tid": TENANT_A_ID,
            "h": KEY_REVOKED_HASH,
            "scopes": json.dumps(["landing_leads:write"]),
            "ts": now_iso,
        })
    return engine


def _make_tenant_engine() -> object:
    import os
    import tempfile
    db_path = os.path.join(tempfile.gettempdir(), "gdx_test_landing_leads.sqlite")
    if os.path.exists(db_path):
        os.unlink(db_path)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS landing_leads (
                id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                name TEXT,
                email TEXT,
                phone TEXT,
                source TEXT,
                message TEXT,
                referrer TEXT,
                utm_campaign TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                status TEXT DEFAULT 'new',
                contacted_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                deleted_at TEXT
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'system',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT,
                user_id TEXT,
                action TEXT,
                entity_type TEXT,
                entity_id TEXT,
                details TEXT,
                ip_address TEXT,
                request_id TEXT,
                row_hash TEXT DEFAULT '',
                prev_hash TEXT DEFAULT '',
                created_at TEXT,
                event_type TEXT,
                actor_id TEXT,
                actor_role TEXT,
                payload TEXT,
                hash TEXT
            )
            """
        ))
    return engine


_FAKE_TENANT_A = {
    "id": TENANT_A_ID,
    "slug": "tenanta",
    "db_url": "sqlite://",
    "subscription_status": "active",
    "db_provisioned": 1,
}

_FAKE_TENANT_FOREIGN = {
    "id": TENANT_FOREIGN_ID,
    "slug": "foreign",
    "db_url": "sqlite://",
    "subscription_status": "active",
    "db_provisioned": 1,
}


# Holder so tests can read directly from the same tenant engine the route writes to
_FIXTURE_STATE: dict = {}


@pytest.fixture(scope="module")
def client():
    """Isolated FastAPI app with public_router only and patched control DB."""
    import importlib
    import sys
    import unittest.mock as _mock

    control_engine = _make_control_engine()
    tenant_engine = _make_tenant_engine()
    _FIXTURE_STATE["tenant_engine"] = tenant_engine

    ControlSession = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)
    TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)

    # Patch tenant lookup
    import gdx_dispatch.core.tenant as _tenant_mod
    _orig_lookup = _tenant_mod._lookup_tenant

    def _patched_lookup(db, slug, tenant_id):
        # Inert under the single-tenant collapse — TenantMiddleware pins
        # single_tenant() and no longer calls _lookup_tenant. Kept (and patched)
        # so this fixture's setup/teardown matches the multi-tenant-era shape;
        # the tenant a request actually gets is always the pinned one.
        if tenant_id == TENANT_FOREIGN_ID or slug == "foreign":
            return _FAKE_TENANT_FOREIGN
        return _FAKE_TENANT_A

    _tenant_mod._lookup_tenant = _patched_lookup

    # Patch SessionLocal in both modules
    import gdx_dispatch.core.database as _db_mod
    _orig_csf = _db_mod.SessionLocal
    _db_mod.SessionLocal = ControlSession  # type: ignore[assignment]

    import gdx_dispatch.core.api_keys as _ak_mod
    _orig_ak_csf = _ak_mod.SessionLocal
    _ak_mod.SessionLocal = ControlSession  # type: ignore[assignment]

    # Stub Redis rate limiter
    async def _noop_check(*args, **kwargs):  # noqa: RUF029
        return True

    async def _noop_get_remaining(*args, **kwargs):  # noqa: RUF029
        return 999

    _mock.patch("gdx_dispatch.core.rate_limiter.RateLimiter.check", new=_noop_check).start()
    _mock.patch(
        "gdx_dispatch.core.rate_limiter.RateLimiter.get_remaining", new=_noop_get_remaining
    ).start()

    # Self-pin single_tenant() to THIS module's tenant so the request-time
    # tenant always matches the keys seeded with TENANT_A_ID. Without this the
    # test depends on the ambient GDX_TENANT_ID, which a sibling module
    # (test_public_api) leaks a mock.patch over — making public_router's
    # cross-tenant guard reject our own keys depending on test order.
    _mock.patch(
        "gdx_dispatch.core.tenant.single_tenant",
        new=lambda: {
            "id": TENANT_A_ID,
            "slug": "gdx",
            "name": "Test Tenant",
            "db_url": "sqlite://",
            "subscription_status": "active",
            "db_provisioned": True,
        },
    ).start()

    # Force fresh import of public_router
    for mod_name in [m for m in list(sys.modules) if m in ("gdx_dispatch.api.public_router", "gdx_dispatch.api")]:
        del sys.modules[mod_name]

    from fastapi import FastAPI

    from gdx_dispatch.core.tenant import TenantMiddleware

    fresh_router_mod = importlib.import_module("gdx_dispatch.api.public_router")
    fresh_router = fresh_router_mod.router

    app = FastAPI()
    app.add_middleware(TenantMiddleware, control_session_factory=ControlSession)
    app.include_router(fresh_router)

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

    _tenant_mod._lookup_tenant = _orig_lookup
    _db_mod.SessionLocal = _orig_csf  # type: ignore[assignment]
    _ak_mod.SessionLocal = _orig_ak_csf  # type: ignore[assignment]
    _mock.patch.stopall()


# ---------------------------------------------------------------------------
# Helpers — a per-test session against the same tenant engine
# ---------------------------------------------------------------------------

def _open_tenant_session():
    """Open a fresh Session bound to the same tenant engine the fixture built."""
    engine = _FIXTURE_STATE["tenant_engine"]
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

URL = "/api/v1/landing-leads"
# Host no longer selects the tenant (single-tenant pin); kept on the happy-path
# request purely as a realistic Host header.
HOST_A = "tenanta.example.com"


class TestPublicLandingLeads:
    def test_happy_path_creates_lead_and_audit_row(self, client: TestClient):
        """Valid key with landing_leads:write + Turnstile fail-open → 201 + row + audit."""
        body = {
            "name": "Alice Test",
            "email": "alice@example.com",
            "phone": "555-0100",
            "message": "Need a new opener",
            "source": "website",
            "utm_campaign": "spring-2026",
        }
        resp = client.post(
            URL,
            json=body,
            headers={
                "X-API-Key": RAW_KEY_A,
                "Origin": "https://www.example.com",
                "Host": HOST_A,
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["status"] == "new"
        lead_id = data["id"]
        assert lead_id

        # Read it back via the same engine the route wrote to
        db = _open_tenant_session()
        try:
            # SQLite stores Uuid(as_uuid=True) as 32-char hex; strip dashes
            row = db.execute(
                text("SELECT name, email, source, company_id, status FROM landing_leads WHERE id = :id"),
                {"id": lead_id.replace("-", "")},
            ).mappings().first()
            assert row is not None
            assert row["name"] == "Alice Test"
            assert row["email"] == "alice@example.com"
            assert row["source"] == "website"
            assert row["company_id"] == TENANT_A_ID
            assert row["status"] == "new"

            # Audit row written with the right prefix + flags
            audit = db.execute(
                text(
                    "SELECT user_id, action, entity_id, details FROM audit_logs "
                    "WHERE entity_id = :id ORDER BY created_at DESC LIMIT 1"
                ),
                # entity_id is written as str(ll.id) which keeps dashes
                {"id": lead_id},
            ).mappings().first()
            assert audit is not None
            assert audit["action"] == "landing_lead_created"
            assert audit["user_id"] == "gdx_live_landi"  # key prefix
            details = json.loads(audit["details"]) if isinstance(audit["details"], str) else audit["details"]
            assert details["turnstile_pass"] is True
            assert details["honeypot_pass"] is True
            assert details["key_prefix"] == "gdx_live_landi"
            assert details["origin"] == "https://www.example.com"
            assert details["source"] == "website"

            # Notification row — broadcast (user_id NULL) so AppTopbar badge
            # query (user_id == uid OR user_id IS NULL) picks it up for every
            # admin/sales user on the tenant.
            notif = db.execute(
                text(
                    "SELECT title, message, category, is_read, tenant_id, user_id "
                    "FROM notifications WHERE tenant_id = :tid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"tid": TENANT_A_ID},
            ).mappings().first()
            assert notif is not None
            assert notif["title"] == "New lead"
            assert "Alice Test" in notif["message"]
            assert notif["category"] == "lead"
            assert notif["is_read"] == 0
            assert notif["user_id"] is None  # broadcast to all users on tenant
        finally:
            db.close()

    def test_missing_api_key_returns_401(self, client: TestClient):
        resp = client.post(URL, json={"name": "Bob", "email": "b@e.com"})
        assert resp.status_code == 401

    def test_unknown_api_key_returns_401(self, client: TestClient):
        resp = client.post(
            URL,
            json={"name": "Bob", "email": "b@e.com"},
            headers={"X-API-Key": "gdx_live_does_not_exist_anywhere"},
        )
        assert resp.status_code == 401

    def test_revoked_key_returns_401(self, client: TestClient):
        resp = client.post(
            URL,
            json={"name": "Bob", "email": "b@e.com"},
            headers={"X-API-Key": RAW_KEY_REVOKED},
        )
        assert resp.status_code == 401

    def test_key_without_scope_returns_403(self, client: TestClient):
        # Key B belongs to the single pinned tenant, so the cross-tenant guard
        # passes and the 403 we get is from the missing landing_leads:write
        # scope — exactly what this test is asserting.
        resp = client.post(
            URL,
            json={"name": "Bob", "email": "b@e.com"},
            headers={"X-API-Key": RAW_KEY_B_NOSCOPE},
        )
        assert resp.status_code == 403
        body = resp.json()
        # scope_required (the canonical helper) raises with detail string
        # "Scope 'landing_leads:write' required. Your key has: [...]"
        detail = body.get("detail", "")
        assert "landing_leads:write" in str(detail)

    def test_honeypot_filled_returns_201_silently_no_insert(self, client: TestClient):
        body = {
            "name": "Bot",
            "email": "spam@example.com",
            "website": "https://spam.example.com",  # honeypot — bots fill this
        }
        resp = client.post(
            URL,
            json=body,
            headers={"X-API-Key": RAW_KEY_A, "Host": HOST_A},
        )
        assert resp.status_code == 201
        # Audit §5: synthetic UUID instead of null so consumers don't crash on
        # `redirectTo('/leads/' + data.id)`. Bot can't tell the difference
        # because no real row was created.
        synth_id = resp.json()["data"]["id"]
        assert synth_id and len(synth_id) == 36 and synth_id.count("-") == 4

        # Verify NO row was created (id was synthetic, never inserted)
        db = _open_tenant_session()
        try:
            count = db.execute(
                text("SELECT COUNT(*) AS c FROM landing_leads WHERE email = :e"),
                {"e": "spam@example.com"},
            ).mappings().first()
            assert count["c"] == 0
            # And the synthetic id has no row either
            also_missing = db.execute(
                text("SELECT 1 FROM landing_leads WHERE id = :i"),
                {"i": synth_id.replace("-", "")},
            ).first()
            assert also_missing is None
        finally:
            db.close()

    def test_turnstile_failure_returns_400(self, client: TestClient):
        """When TURNSTILE_SECRET is set and Cloudflare returns success=false → 400."""
        async def _fake_verify(token, ip, *, expected_hostname=None):
            return False, ["invalid-input-response"]

        with patch("gdx_dispatch.core.turnstile.verify_turnstile", new=_fake_verify):
            # Re-import the route module so the patched name is bound at call time.
            # The route does an in-handler `from gdx_dispatch.core.turnstile import verify_turnstile`
            # so the patch applied to the source module is picked up.
            resp = client.post(
                URL,
                json={
                    "name": "Carol",
                    "email": "c@e.com",
                    "cf_turnstile_token": "fake-token",
                },
                headers={"X-API-Key": RAW_KEY_A, "Host": HOST_A},
            )
        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error") == "challenge_failed"

    def test_key_bound_to_foreign_tenant_is_rejected_403(self, client: TestClient):
        """Retained cross-tenant guard, reframed for single-tenant.

        Pre-collapse this asserted that a tenant-A key POSTed to tenant B's
        subdomain was rejected. Under the single-tenant pin every request
        resolves to the one tenant, so the guard's job is now narrower but still
        load-bearing: a key whose ``tenant_id`` does not match the pinned tenant
        (a stale/misissued key) must be rejected by ``_require_api_key`` before
        ``Depends(get_db)`` opens a connection and stamps the wrong owner.
        """
        body = {"name": "Dora", "email": "d@e.com"}
        resp = client.post(
            URL,
            json=body,
            headers={"X-API-Key": RAW_KEY_FOREIGN},
        )
        assert resp.status_code == 403, resp.text
        # Single-tenant invariant message (was "tenant subdomain" pre-collapse):
        # a key bound to any company id other than the pinned one is rejected.
        assert "does not belong" in str(resp.json().get("detail", "")).lower()

        # And confirm: no row landed in either tenant's table.
        db = _open_tenant_session()
        try:
            count = db.execute(
                text("SELECT COUNT(*) AS c FROM landing_leads WHERE email = :e"),
                {"e": "d@e.com"},
            ).mappings().first()
            assert count["c"] == 0
        finally:
            db.close()


class TestTurnstileHelper:
    """Direct unit tests for gdx_dispatch.core.turnstile.verify_turnstile."""

    def test_fail_open_when_secret_unset(self, monkeypatch):
        import asyncio
        monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
        from gdx_dispatch.core.turnstile import verify_turnstile

        ok, errs = asyncio.run(
            verify_turnstile("any-token", "1.2.3.4")
        )
        assert ok is True
        assert errs == []

    def test_missing_token_returns_false_when_secret_set(self, monkeypatch):
        import asyncio
        monkeypatch.setenv("TURNSTILE_SECRET", "sekret")
        from gdx_dispatch.core.turnstile import verify_turnstile

        ok, errs = asyncio.run(
            verify_turnstile(None, "1.2.3.4")
        )
        assert ok is False
        assert "missing-input-response" in errs

    def test_success_response_returns_ok(self, monkeypatch):
        import asyncio

        import httpx
        monkeypatch.setenv("TURNSTILE_SECRET", "sekret")

        from gdx_dispatch.core import turnstile

        async def _fake_post(*args, **kwargs):
            return httpx.Response(200, json={"success": True, "hostname": "x.com"})

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return await _fake_post()

        monkeypatch.setattr(turnstile.httpx, "AsyncClient", _FakeClient)

        ok, errs = asyncio.run(
            turnstile.verify_turnstile("token", "1.2.3.4")
        )
        assert ok is True
        assert errs == []

    def test_failure_response_returns_error_codes(self, monkeypatch):
        import asyncio

        import httpx
        monkeypatch.setenv("TURNSTILE_SECRET", "sekret")

        from gdx_dispatch.core import turnstile

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                return httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]})

        monkeypatch.setattr(turnstile.httpx, "AsyncClient", _FakeClient)

        ok, errs = asyncio.run(
            turnstile.verify_turnstile("token", "1.2.3.4")
        )
        assert ok is False
        assert "timeout-or-duplicate" in errs

    def test_network_error_returns_false(self, monkeypatch):
        import asyncio

        import httpx
        monkeypatch.setenv("TURNSTILE_SECRET", "sekret")

        from gdx_dispatch.core import turnstile

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(turnstile.httpx, "AsyncClient", _FakeClient)

        ok, errs = asyncio.run(
            turnstile.verify_turnstile("token", "1.2.3.4")
        )
        assert ok is False
        assert "network-error" in errs

    def test_hostname_mismatch_when_pinned(self, monkeypatch):
        import asyncio

        import httpx
        monkeypatch.setenv("TURNSTILE_SECRET", "sekret")
        monkeypatch.setenv("TURNSTILE_HOSTNAME", "example.com")

        from gdx_dispatch.core import turnstile

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                return httpx.Response(200, json={"success": True, "hostname": "evil.com"})

        monkeypatch.setattr(turnstile.httpx, "AsyncClient", _FakeClient)

        ok, errs = asyncio.run(
            turnstile.verify_turnstile("token", "1.2.3.4")
        )
        assert ok is False
        assert "hostname-mismatch" in errs
