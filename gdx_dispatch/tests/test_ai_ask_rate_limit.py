"""Sprint 1.x-S19 — /api/ai/ask rate limit contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.ai import router as ai_router
from gdx_dispatch.routers.ai import get_current_principal_for_ai, get_db_for_ai


@dataclass
class _Admin:
    identity_id: Any = field(default_factory=uuid4)
    tenant_id: str = "11111111-1111-1111-1111-111111111111"
    principal_role: str = "admin"
    capabilities: tuple = (("read", "customer"),)
    auth_kind: str = "session"
    actor_type: str = "human"
    delegated_by_user_id: str | None = None
    is_super_admin: bool = False
    is_restricted: bool = False


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """Reset the in-memory rate-limit bucket between tests so they don't
    pollute each other. Implementer must expose a hook (e.g. a
    `_reset_rate_limit()` callable in gdx_dispatch.routers.ai) — no global state
    pollution between test cases is the contract."""
    from gdx_dispatch.routers import ai as ai_mod
    if hasattr(ai_mod, "_reset_rate_limit"):
        ai_mod._reset_rate_limit()
    yield
    if hasattr(ai_mod, "_reset_rate_limit"):
        ai_mod._reset_rate_limit()


@pytest.fixture
def app_and_client():
    app = FastAPI()
    app.include_router(ai_router)
    app.dependency_overrides[get_current_principal_for_ai] = lambda: _Admin()
    app.dependency_overrides[get_db_for_ai] = lambda: MagicMock()
    return TestClient(app)


def _resp_text(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    return resp


def test_31st_request_in_a_minute_returns_429(app_and_client):
    """30 successful requests, then the 31st returns 429."""
    client = app_and_client
    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client:
        fake = MagicMock()
        fake.messages.create.return_value = _resp_text("ok")
        mock_get_client.return_value = fake

        for i in range(30):
            r = client.post("/api/ai/ask", json={"question": f"q{i}"})
            assert r.status_code == 200, f"request {i} returned {r.status_code}: {r.text}"

        r = client.post("/api/ai/ask", json={"question": "31st"})
    assert r.status_code == 429, r.text
    body = r.json()
    assert body.get("detail") == "rate_limit_exceeded"
    assert "retry_after_s" in body


def test_disabled_path_does_not_count_against_limit(app_and_client):
    """The disabled-when-no-key path does not consume the bucket — that
    path is a cheap config-check and shouldn't gate user attempts.
    (If the implementer chooses to count it, that's defensible — change
    this test to match. The contract here is "consistent with the
    implementer's choice"; we just assert that 100 disabled-key requests
    don't return 429.)"""
    client = app_and_client
    with patch("gdx_dispatch.routers.ai.get_key", return_value=None):
        for i in range(100):
            r = client.post("/api/ai/ask", json={"question": f"q{i}"})
            assert r.status_code == 200, f"disabled-path request {i} returned {r.status_code}"


def test_separate_tenants_have_separate_buckets(app_and_client):
    """A tenant being rate-limited must NOT affect another tenant's bucket."""
    client = app_and_client

    # Override the principal dep to vary tenant_id per request via a closure.
    app = client.app
    tenant_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tenant_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    current_tenant = {"tid": tenant_a}

    def _principal_factory():
        admin = _Admin()
        admin.tenant_id = current_tenant["tid"]
        return admin

    app.dependency_overrides[get_current_principal_for_ai] = _principal_factory

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client:
        fake = MagicMock()
        fake.messages.create.return_value = _resp_text("ok")
        mock_get_client.return_value = fake

        # Burn tenant_a's bucket.
        for i in range(30):
            r = client.post("/api/ai/ask", json={"question": f"a{i}"})
            assert r.status_code == 200
        r = client.post("/api/ai/ask", json={"question": "a31"})
        assert r.status_code == 429

        # Switch to tenant_b — should still have a fresh bucket.
        current_tenant["tid"] = tenant_b
        r = client.post("/api/ai/ask", json={"question": "b1"})
        assert r.status_code == 200, r.text
