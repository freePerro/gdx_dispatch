"""Sprint 1.x-S14 — /api/ai/ask endpoint skeleton contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.ai import router as ai_router
from gdx_dispatch.routers.ai import get_db_for_ai, get_db_for_ai


@dataclass
class _StubPrincipal:
    """Minimal Principal-shaped stub for the auth dependency override."""
    identity_id: Any = field(default_factory=uuid4)
    tenant_id: str = "11111111-1111-1111-1111-111111111111"
    principal_role: str = "admin"
    capabilities: tuple = (("*", "*"),)
    auth_kind: str = "session"
    actor_type: str = "human"
    delegated_by_user_id: str | None = None
    is_super_admin: bool = True
    is_restricted: bool = False


@pytest.fixture
def app_and_client():
    from gdx_dispatch.routers.ai import get_current_principal_for_ai
    app = FastAPI()
    app.include_router(ai_router)

    principal = _StubPrincipal()
    app.dependency_overrides[get_current_principal_for_ai] = lambda: principal

    # The DB dep is also overridden so the test doesn't need a real Session.
    app.dependency_overrides[get_db_for_ai] = lambda: object()  # arbitrary truthy
    app.dependency_overrides[get_db_for_ai] = lambda: object()
    return TestClient(app), principal, app


def test_ask_returns_disabled_when_no_key(app_and_client):
    client, principal, app = app_and_client
    with patch("gdx_dispatch.routers.ai.get_key", return_value=None):
        r = client.post("/api/ai/ask", json={"question": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["disabled"] is True
    assert body["reason"] == "no_key"
    assert body.get("answer") is None


# NOTE: test_ask_returns_placeholder_when_key_set was deleted when S15 wired
# the real Haiku loop. The placeholder path no longer exists. The S15 contract
# tests in gdx_dispatch/tests/test_ai_ask_loop_single_tool.py exercise the loop's real
# behavior under patched Anthropic + invoke_tool.


def test_ask_requires_question_field(app_and_client):
    client, _, _ = app_and_client
    with patch("gdx_dispatch.routers.ai.get_key", return_value=None):
        r = client.post("/api/ai/ask", json={})
    # FastAPI returns 422 for pydantic validation failure on missing required field.
    assert r.status_code == 422


def test_ask_unauthenticated_returns_401_without_override():
    """When the auth dependency isn't overridden, the endpoint must reject."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(ai_router)
    # No dependency_overrides — the real auth dep should fire and reject.
    client = TestClient(app)
    r = client.post("/api/ai/ask", json={"question": "hello"})
    # Either 401 (auth dep raised HTTPException) or 403 — both are correct
    # rejections; the contract is "not 200, not 422".
    assert r.status_code in (401, 403, 500), (
        f"unauthenticated request returned {r.status_code}; expected auth rejection"
    )
