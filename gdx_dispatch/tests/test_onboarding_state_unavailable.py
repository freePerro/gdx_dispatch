"""GDXA-344: an onboarding state-layer failure is an error, never empty progress.

``GET /api/onboarding`` used to wrap its body in ``except Exception`` and answer
200 with ``complete: 0, steps: []``, and the Redis helpers under it swallowed a
Redis failure into a per-process dict, so with Redis down the tenant read back
as having completed nothing and every write vanished on restart. These tests
fail if either swallow comes back.
"""
from __future__ import annotations

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core import onboarding


class _DownRedis:
    def __getattr__(self, _name):
        def _fail(*_a, **_k):
            raise ConnectionError("redis down")

        return _fail


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(onboarding.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def _unreachable(monkeypatch):
    # Exercise the real _get_redis: point it at a port nothing listens on.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")


def test_unreachable_redis_is_503_not_empty_progress(monkeypatch):
    _unreachable(monkeypatch)
    resp = _client().get("/api/onboarding")
    assert resp.status_code == 503, resp.text
    assert "steps" not in resp.json()


def test_redis_error_after_connect_is_503(monkeypatch):
    monkeypatch.setattr(onboarding, "_get_redis", lambda: _DownRedis())
    resp = _client().get("/api/onboarding")
    assert resp.status_code == 503, resp.text


def test_non_store_failure_is_500_not_200(monkeypatch):
    """The issue's repro: STEP_TITLES emptied used to give 200 with steps: []."""
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    monkeypatch.setattr(onboarding, "_get_redis", lambda: client)
    monkeypatch.setattr(onboarding, "STEP_TITLES", {})
    resp = _client().get("/api/onboarding")
    assert resp.status_code == 500, resp.text


def test_write_with_redis_down_raises_instead_of_vanishing(monkeypatch):
    _unreachable(monkeypatch)
    with pytest.raises(onboarding.OnboardingStateUnavailable):
        onboarding.complete_step("t", "company_info")
    with pytest.raises(onboarding.OnboardingStateUnavailable):
        onboarding.reset_onboarding("t")


def test_corrupt_stored_state_is_unavailable_not_empty(monkeypatch):
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    client.set(onboarding._redis_key("t"), "{not json")
    monkeypatch.setattr(onboarding, "_get_redis", lambda: client)
    with pytest.raises(onboarding.OnboardingStateUnavailable):
        onboarding.get_onboarding_status("t")


def test_healthy_store_round_trips_through_the_route(monkeypatch):
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    monkeypatch.setattr(onboarding, "_get_redis", lambda: client)
    onboarding.complete_step("unknown", "company_info")
    resp = _client().get("/api/onboarding")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["complete"] == 1
    assert body["next_step"] == "first_technician"
    assert len(body["steps"]) == len(onboarding.WIZARD_STEPS)


# ── Sibling sweep (GDXA-344): platform-core routes that answered an empty list ──
# on a failed read. Both now let the failure surface as a 500.


class _BrokenSession:
    def execute(self, *_a, **_k):
        raise RuntimeError("db down")

    def query(self, *_a, **_k):
        raise RuntimeError("db down")

    def rollback(self):
        pass


def test_list_tax_jurisdictions_db_failure_is_500_not_empty():
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers import admin_settings
    from gdx_dispatch.routers.auth import get_current_user

    app = FastAPI()
    app.include_router(admin_settings.router)
    app.dependency_overrides[get_db] = lambda: _BrokenSession()
    app.dependency_overrides[get_current_user] = lambda: {"sub": "u", "role": "admin", "tenant_id": "t"}
    for dep in admin_settings.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    resp = TestClient(app, raise_server_exceptions=False).get("/api/admin/tax-jurisdictions")
    assert resp.status_code == 500, resp.text


def test_list_api_keys_db_failure_is_500_not_empty():
    from uuid import uuid4

    from gdx_dispatch.core import api_keys

    app = FastAPI()
    app.include_router(api_keys.router)
    app.dependency_overrides[api_keys._get_db_session] = lambda: _BrokenSession()
    app.dependency_overrides[api_keys._get_current_user_safe] = lambda: {"tenant_id": str(uuid4())}
    for dep in api_keys.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    resp = TestClient(app, raise_server_exceptions=False).get("/api/developer/keys")
    assert resp.status_code == 500, resp.text
