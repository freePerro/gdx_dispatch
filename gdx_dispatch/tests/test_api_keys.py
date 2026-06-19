"""Tests for API key management and public API authentication.

Tests:
  1. create key — returns full key, prefix, id
  2. key shown only once — list returns prefix only, not full key
  3. list keys — shows prefix only, not key_hash
  4. revoke key — sets revoked_at, subsequent use rejected
  5. invalid key rejected — 401
  6. valid key grants access to public API endpoint
  7. scope enforcement — missing scope returns 403
  8. rate limiting — 61st request returns 429
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

import gdx_dispatch.core.api_keys as ak_module
from gdx_dispatch.core.api_keys import (
    APIKey,
    APIKeyBase,
    APIKeyMiddleware,
    _get_db_session,
    _get_current_user_safe,
    generate_api_key,
    hash_key,
    scope_required,
    verify_api_key,
)
from gdx_dispatch.core.api_keys import (
    router as api_keys_router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def control_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    APIKeyBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def control_db(control_engine):
    Session = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)
    db = Session()
    yield db
    db.close()


def _make_api_key(db, tenant_id=None, scopes=None, revoked=False, expired=False):
    """Helper — insert a test APIKey and return (raw_key, api_key_row)."""
    raw_key, key_hash_, key_prefix = generate_api_key()
    now = datetime.now(UTC)
    # expires_at: naive UTC for SQLite compat (strip tzinfo so verify_api_key re-attaches it)
    expires_at = None
    if expired:
        expires_at = (now - timedelta(days=1)).replace(tzinfo=None)
    if isinstance(tenant_id, UUID):
        tid = tenant_id
    elif tenant_id:
        tid = UUID(str(tenant_id))
    else:
        tid = uuid4()
    api_key = APIKey(
        id=uuid4(),
        tenant_id=tid,
        key_hash=key_hash_,
        key_prefix=key_prefix,
        name="Test Key",
        scopes=scopes or ["read:jobs"],
        created_at=now.replace(tzinfo=None),
        expires_at=expires_at,
        revoked_at=now.replace(tzinfo=None) if revoked else None,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return raw_key, api_key


def _make_router_app(control_engine, tenant_id_str: str) -> TestClient:
    """Build a minimal FastAPI app with the api_keys router, overriding DB and auth."""
    app = FastAPI()
    app.include_router(api_keys_router)

    Session = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    def _override_user() -> dict:
        return {"user_id": "u1", "tenant_id": tenant_id_str, "role": "admin"}

    app.dependency_overrides[_get_db_session] = _override_db
    app.dependency_overrides[_get_current_user_safe] = _override_user
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestGenerateApiKey:
    def test_format(self):
        raw_key, key_hash_, key_prefix = generate_api_key()
        assert raw_key.startswith("gdx_live_"), "key must start with gdx_live_"
        assert len(raw_key) == 41, f"expected 41 chars, got {len(raw_key)}"  # gdx_live_(9) + 32 hex
        assert key_prefix == raw_key[:16]
        assert key_hash_ == hashlib.sha256(raw_key.encode()).hexdigest()

    def test_uniqueness(self):
        keys = {generate_api_key()[0] for _ in range(20)}
        assert len(keys) == 20, "all generated keys must be unique"


class TestHashKey:
    def test_sha256(self):
        raw = "gdx_live_abc123"
        assert hash_key(raw) == hashlib.sha256(raw.encode()).hexdigest()

    def test_deterministic(self):
        assert hash_key("x") == hash_key("x")
        assert hash_key("x") != hash_key("y")


class TestVerifyApiKey:
    def test_valid_key(self, control_db):
        raw_key, api_key = _make_api_key(control_db, scopes=["read:jobs"])
        result = verify_api_key(control_db, raw_key)
        assert result is not None
        assert result.id == api_key.id

    def test_invalid_key_returns_none(self, control_db):
        assert verify_api_key(control_db, "gdx_live_doesnotexist") is None

    def test_revoked_key_returns_none(self, control_db):
        raw_key, _ = _make_api_key(control_db, revoked=True)
        assert verify_api_key(control_db, raw_key) is None

    def test_expired_key_returns_none(self, control_db):
        raw_key, _ = _make_api_key(control_db, expired=True)
        assert verify_api_key(control_db, raw_key) is None


# ---------------------------------------------------------------------------
# Test 1 — create key returns full key, prefix, id
# ---------------------------------------------------------------------------


class TestCreateApiKey:
    def test_create_returns_full_key(self, control_engine):
        tenant_id = str(uuid4())
        client = _make_router_app(control_engine, tenant_id)
        r = client.post(
            "/api/developer/keys",
            json={"name": "My Integration", "scopes": ["read:jobs", "read:customers"]},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert "key" in data, "full key must be present on creation"
        assert data["key"].startswith("gdx_live_"), "key must have correct prefix"
        assert "prefix" in data
        assert "id" in data


# ---------------------------------------------------------------------------
# Test 2 — key shown only once (list never reveals full key)
# ---------------------------------------------------------------------------


class TestKeyShownOnce:
    def test_list_does_not_expose_key_hash(self, control_db, control_engine):
        tid = uuid4()
        raw_key, api_key = _make_api_key(control_db, tenant_id=tid, scopes=["read:jobs"])
        client = _make_router_app(control_engine, str(tid))

        r = client.get("/api/developer/keys")
        assert r.status_code == 200, r.text
        keys = r.json()["data"]
        assert len(keys) >= 1
        for k in keys:
            assert "key_hash" not in k, "key_hash must never be returned in list"
            # Full key must not appear in any field value
            for v in k.values():
                assert v != raw_key, "full raw key must never appear in list response"
            assert "key_prefix" in k, "key_prefix must be present for display"


# ---------------------------------------------------------------------------
# Test 3 — list keys shows prefix only
# ---------------------------------------------------------------------------


class TestListKeys:
    def test_prefix_only_in_list(self, control_db, control_engine):
        tid = uuid4()
        raw_key, api_key = _make_api_key(control_db, tenant_id=tid, scopes=["read:jobs", "write:jobs"])
        client = _make_router_app(control_engine, str(tid))

        r = client.get("/api/developer/keys")
        assert r.status_code == 200
        keys = r.json()["data"]
        found = next((k for k in keys if k["id"] == str(api_key.id)), None)
        assert found is not None
        assert found["key_prefix"] == api_key.key_prefix
        assert found["key_prefix"] != raw_key, "prefix must not equal full key"
        assert len(found["key_prefix"]) < len(raw_key)


# ---------------------------------------------------------------------------
# Test 4 — revoke key
# ---------------------------------------------------------------------------


class TestRevokeKey:
    def test_revoke_sets_revoked_at(self, control_db, control_engine):
        tid = uuid4()
        raw_key, api_key = _make_api_key(control_db, tenant_id=tid, scopes=["read:jobs"])
        client = _make_router_app(control_engine, str(tid))

        r = client.delete(f"/api/developer/keys/{api_key.id}")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        # Verify key is revoked in DB
        control_db.expire(api_key)
        control_db.refresh(api_key)
        assert api_key.revoked_at is not None, "revoked_at must be set after revoke"

        # Verify verify_api_key now returns None
        assert verify_api_key(control_db, raw_key) is None, "revoked key must not verify"


# ---------------------------------------------------------------------------
# Test 5 — invalid key rejected (401)
# ---------------------------------------------------------------------------


class TestInvalidKeyRejected:
    def test_invalid_key_returns_401(self, control_db, control_engine):
        _, _ = _make_api_key(control_db)  # populate DB so lookup fails cleanly
        app = FastAPI()
        app.add_middleware(APIKeyMiddleware)

        @app.get("/v1/test")
        async def _test():
            return JSONResponse({"ok": True})

        Session = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)

        # Patch module-level SessionLocal so middleware uses the test DB
        with patch.object(ak_module, "SessionLocal", side_effect=lambda: Session()):
            client = TestClient(app, raise_server_exceptions=False)
            r = client.get("/v1/test", headers={"X-API-Key": "gdx_live_invalid_key_xyz"})
            assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Test 6 — valid key grants access
# ---------------------------------------------------------------------------


class TestValidKeyGrantsAccess:
    def test_valid_key_sets_tenant_state(self, control_db, control_engine):
        tid = uuid4()
        raw_key, api_key = _make_api_key(control_db, tenant_id=tid, scopes=["read:jobs"])
        captured: dict = {}

        app = FastAPI()
        app.add_middleware(APIKeyMiddleware)

        @app.get("/v1/test")
        async def _test(request: Request):
            captured["tenant_id"] = getattr(request.state, "api_key_tenant_id", None)
            captured["scopes"] = getattr(request.state, "api_key_scopes", [])
            return JSONResponse({"ok": True})

        Session = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1
        mock_redis.expire.return_value = True

        with patch.object(ak_module, "SessionLocal", side_effect=lambda: Session()):
            with patch.object(ak_module, "_get_redis", return_value=mock_redis):
                client = TestClient(app, raise_server_exceptions=True)
                r = client.get("/v1/test", headers={"X-API-Key": raw_key})
                assert r.status_code == 200, r.text
                assert captured.get("tenant_id") == str(tid)
                assert "read:jobs" in captured.get("scopes", [])


# ---------------------------------------------------------------------------
# Test 7 — scope enforcement
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    def test_missing_scope_returns_403(self):
        app = FastAPI()

        @app.get("/v1/restricted")
        async def _restricted(request: Request, _scope=scope_required("write:jobs")):
            return JSONResponse({"ok": True})

        client = TestClient(app, raise_server_exceptions=False)
        # No scopes set on request.state → 403
        r = client.get("/v1/restricted")
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

    def test_correct_scope_passes(self):
        app = FastAPI()

        @app.get("/v1/jobs-read")
        async def _jobs(request: Request, _scope=scope_required("read:jobs")):
            return JSONResponse({"ok": True})

        # Inject scopes via middleware before the endpoint runs
        class InjectScopeMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, req, call_next):
                req.state.api_key_scopes = ["read:jobs", "read:customers"]
                return await call_next(req)

        app.add_middleware(InjectScopeMiddleware)
        client = TestClient(app, raise_server_exceptions=True)
        r = client.get("/v1/jobs-read")
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Test 8 — rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_61st_request_returns_429(self, control_db, control_engine):
        tid = uuid4()
        raw_key, api_key = _make_api_key(control_db, tenant_id=tid, scopes=["read:jobs"])
        app = FastAPI()
        app.add_middleware(APIKeyMiddleware)

        @app.get("/v1/rate-test")
        async def _test():
            return JSONResponse({"ok": True})

        Session = sessionmaker(bind=control_engine, autoflush=False, autocommit=False)

        # Simulate Redis counter that increments per call
        call_count = [0]

        def _mock_incr(key):
            call_count[0] += 1
            return call_count[0]

        mock_redis = MagicMock()
        mock_redis.incr.side_effect = _mock_incr
        mock_redis.expire.return_value = True

        with patch.object(ak_module, "SessionLocal", side_effect=lambda: Session()):
            with patch.object(ak_module, "_get_redis", return_value=mock_redis):
                client = TestClient(app, raise_server_exceptions=False)
                # First 60 requests should succeed
                for i in range(60):
                    r = client.get("/v1/rate-test", headers={"X-API-Key": raw_key})
                    assert r.status_code == 200, f"request {i + 1} failed: {r.status_code} {r.text}"
                # 61st should be rate-limited
                r = client.get("/v1/rate-test", headers={"X-API-Key": raw_key})
                assert r.status_code == 429, f"expected 429 on 61st request, got {r.status_code}"
