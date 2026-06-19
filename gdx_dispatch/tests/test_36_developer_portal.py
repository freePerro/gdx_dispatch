"""
gdx_dispatch/tests/test_36_developer_portal.py — Developer portal and API key management tests.

Tests:
  1. test_developer_portal_page         — GET /developer returns HTML 200
  2. test_create_api_key                — POST /api/api-keys returns key + id
  3. test_list_api_keys                 — GET /api/api-keys returns list (prefix only)
  4. test_revoke_api_key                — DELETE /api/api-keys/{id} soft-revokes key
  5. test_api_key_usage_log             — GET /api/api-keys/{id}/usage returns usage data
  6. test_api_key_tenant_isolated       — tenant B cannot delete/view tenant A's key
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.api_keys import APIKeyBase
from gdx_dispatch.core.developer_portal import router as dev_portal_router

# ---------------------------------------------------------------------------
# Tenant/user fixtures
# ---------------------------------------------------------------------------

TENANT_A_ID = str(uuid.uuid4())
TENANT_B_ID = str(uuid.uuid4())

_FAKE_USER_A = {"tenant_id": TENANT_A_ID, "sub": "user-a@example.com", "role": "admin"}
_FAKE_USER_B = {"tenant_id": TENANT_B_ID, "sub": "user-b@example.com", "role": "admin"}


# ---------------------------------------------------------------------------
# In-memory control DB
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def control_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    APIKeyBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def ControlSession(control_engine):
    return sessionmaker(bind=control_engine, autoflush=False, autocommit=False)


# ---------------------------------------------------------------------------
# App factory — injects fake auth for a given user
# ---------------------------------------------------------------------------


def _make_app(ControlSession, fake_user: dict) -> FastAPI:
    """Build a minimal FastAPI app with developer_portal router and fake auth."""
    import gdx_dispatch.core.api_keys as _ak_mod

    app = FastAPI()
    app.include_router(dev_portal_router)

    # Override _get_db so routes use our in-memory session
    def _fake_control_db():
        db = ControlSession()
        try:
            yield db
        finally:
            db.close()

    # Override _require_authenticated_user so routes see our fake_user
    async def _fake_auth(request: Request) -> dict:
        return fake_user

    from gdx_dispatch.core.developer_portal import _get_db, _require_authenticated_user
    app.dependency_overrides[_get_db] = _fake_control_db
    app.dependency_overrides[_require_authenticated_user] = _fake_auth

    # Patch SessionLocal in api_keys and developer_portal so middleware helpers work
    _ak_mod.SessionLocal = ControlSession  # type: ignore[assignment]

    return app


# ---------------------------------------------------------------------------
# Fixtures — one client per tenant
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client_a(ControlSession):
    app = _make_app(ControlSession, _FAKE_USER_A)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def client_b(ControlSession):
    app = _make_app(ControlSession, _FAKE_USER_B)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeveloperPortalPage:
    def test_developer_portal_page(self, client_a: TestClient):
        """GET /developer should return 200 HTML (even if template missing)."""
        resp = client_a.get("/developer")
        assert resp.status_code == 200
        content_type = resp.headers.get("content-type", "")
        assert "html" in content_type, f"Expected HTML content-type, got: {content_type}"


class TestCreateAPIKey:
    def test_create_api_key(self, client_a: TestClient):
        """POST /api/api-keys returns 201 with full key, prefix, id, and scopes."""
        resp = client_a.post(
            "/api/api-keys",
            json={
                "name": "My Integration Key",
                "scopes": ["read:jobs", "write:jobs"],
            },
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:400]}"
        body = resp.json()
        assert "key" in body, f"Missing 'key' in response: {body}"
        assert "id" in body
        assert "prefix" in body
        assert "scopes" in body
        assert "read:jobs" in body["scopes"]
        # Key should start with the standard prefix
        assert body["key"].startswith("gdx_live_"), f"Unexpected key format: {body['key'][:20]}"


class TestListAPIKeys:
    def test_list_api_keys(self, client_a: TestClient):
        """GET /api/api-keys returns a list of keys with prefix (never full key)."""
        # Ensure at least one key exists
        client_a.post(
            "/api/api-keys",
            json={"name": "List Test Key", "scopes": ["read:customers"]},
        )
        resp = client_a.get("/api/api-keys")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:400]}"
        body = resp.json()
        assert "data" in body, f"Missing 'data' key: {body}"
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 1

        for item in body["data"]:
            # Full key must never appear in the list response
            assert "key" not in item or not str(item.get("key", "")).startswith("gdx_live_"), (
                "Full API key must not be returned in list response"
            )
            assert "key_prefix" in item
            assert "id" in item
            assert "name" in item


class TestRevokeAPIKey:
    def test_revoke_api_key(self, client_a: TestClient):
        """DELETE /api/api-keys/{id} soft-revokes an API key."""
        # Create a key to revoke
        create_resp = client_a.post(
            "/api/api-keys",
            json={"name": "Key to Revoke", "scopes": ["read:jobs"]},
        )
        assert create_resp.status_code == 201, f"Setup failed: {create_resp.text[:300]}"
        key_id = create_resp.json()["id"]

        # Revoke it
        revoke_resp = client_a.delete(f"/api/api-keys/{key_id}")
        assert revoke_resp.status_code == 200, (
            f"Expected 200, got {revoke_resp.status_code}: {revoke_resp.text[:400]}"
        )
        body = revoke_resp.json()
        assert body.get("ok") is True
        assert "revoked_at" in body

        # Revoking again should still return ok (idempotent)
        second_revoke = client_a.delete(f"/api/api-keys/{key_id}")
        assert second_revoke.status_code == 200
        assert second_revoke.json().get("ok") is True


class TestAPIKeyUsageLog:
    def test_api_key_usage_log(self, client_a: TestClient):
        """GET /api/api-keys/{id}/usage returns usage data for a key."""
        # Create a key
        create_resp = client_a.post(
            "/api/api-keys",
            json={"name": "Usage Test Key", "scopes": ["read:jobs"]},
        )
        assert create_resp.status_code == 201, f"Setup failed: {create_resp.text[:300]}"
        key_id = create_resp.json()["id"]

        # Check usage
        usage_resp = client_a.get(f"/api/api-keys/{key_id}/usage")
        assert usage_resp.status_code == 200, (
            f"Expected 200, got {usage_resp.status_code}: {usage_resp.text[:400]}"
        )
        body = usage_resp.json()
        assert "data" in body
        data = body["data"]
        assert data["id"] == key_id
        assert "last_used_at" in data
        assert "created_at" in data
        assert "scopes" in data
        assert "usage_log" in data
        assert isinstance(data["usage_log"], list)


class TestAPIKeyTenantIsolation:
    def test_api_key_tenant_isolated(self, client_a: TestClient, client_b: TestClient):
        """Tenant B cannot delete or view tenant A's API key."""
        # Tenant A creates a key
        create_resp = client_a.post(
            "/api/api-keys",
            json={"name": "Tenant A Private Key", "scopes": ["read:jobs"]},
        )
        assert create_resp.status_code == 201, f"Setup failed: {create_resp.text[:300]}"
        key_id = create_resp.json()["id"]

        # Tenant B attempts to revoke tenant A's key — must get 404
        delete_resp = client_b.delete(f"/api/api-keys/{key_id}")
        assert delete_resp.status_code == 404, (
            f"Tenant isolation breach: tenant B deleted tenant A's key "
            f"(status={delete_resp.status_code})"
        )

        # Tenant B attempts to view usage for tenant A's key — must get 404
        usage_resp = client_b.get(f"/api/api-keys/{key_id}/usage")
        assert usage_resp.status_code == 404, (
            f"Tenant isolation breach: tenant B viewed tenant A's key usage "
            f"(status={usage_resp.status_code})"
        )
