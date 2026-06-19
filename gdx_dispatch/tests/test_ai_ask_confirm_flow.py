"""Sprint 1.x-S18 — confirm + apply flow."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.ai import router as ai_router
from gdx_dispatch.routers.ai import get_current_principal_for_ai, get_db_for_ai, get_db_for_ai


@dataclass
class _Admin:
    identity_id: Any = field(default_factory=uuid4)
    tenant_id: str = "11111111-1111-1111-1111-111111111111"
    principal_role: str = "admin"
    capabilities: tuple = (("read", "customer"), ("write", "customer.contact"))
    auth_kind: str = "session"
    actor_type: str = "human"
    delegated_by_user_id: str | None = None
    is_super_admin: bool = False
    is_restricted: bool = False


@pytest.fixture
def app_and_client():
    app = FastAPI()
    app.include_router(ai_router)
    app.dependency_overrides[get_current_principal_for_ai] = lambda: _Admin()
    app.dependency_overrides[get_db_for_ai] = lambda: MagicMock()
    app.dependency_overrides[get_db_for_ai] = lambda: MagicMock()
    return TestClient(app)


def test_confirm_apply_invokes_tool_with_approval_ref(app_and_client):
    """A confirm payload must call invoke_tool with approval_ref set; the
    response carries the tool's success result."""
    client = app_and_client

    captured: dict[str, Any] = {}

    async def _fake_invoke(name, payload, *, principal, db, approval_ref=None, **kw):
        captured["name"] = name
        captured["payload"] = payload
        captured["approval_ref"] = approval_ref
        res = MagicMock()
        res.ok = True
        res.result = {"updated": True}
        res.error_type = None
        res.error_body = None
        return res

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.invoke_tool", side_effect=_fake_invoke):
        r = client.post(
            "/api/ai/ask",
            json={
                "question": "(confirm)",
                "approval_ref": "tok_abc",
                "tool": "customers.mark_contacted",
                "payload": {"customer_id": "cust-1"},
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("disabled") is not True
    # invoke_tool was called with approval_ref.
    assert captured["approval_ref"] == "tok_abc"
    assert captured["name"] == "customers.mark_contacted"
    assert captured["payload"] == {"customer_id": "cust-1"}
    # Response carries the success result.
    assert body.get("result") == {"updated": True} or body.get("answer")
    assert "customers.mark_contacted" in body.get("tools_used", [])


def test_confirm_without_tool_or_payload_returns_400(app_and_client):
    """Bare `approval_ref` without tool/payload is a malformed request."""
    client = app_and_client
    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"):
        r = client.post(
            "/api/ai/ask",
            json={"question": "(confirm)", "approval_ref": "tok_abc"},
        )
    # Pydantic validation OR a structured 400 is acceptable; the contract is
    # "not a 200 success and not a generic 500".
    assert r.status_code in (400, 422), r.text


def test_confirm_apply_does_not_call_anthropic(app_and_client):
    """The confirm path skips the Haiku loop entirely — no LLM call."""
    client = app_and_client

    async def _fake_invoke(*args, **kwargs):
        res = MagicMock()
        res.ok = True
        res.result = {"updated": True}
        res.error_type = None
        res.error_body = None
        return res

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client, \
         patch("gdx_dispatch.routers.ai.invoke_tool", side_effect=_fake_invoke):
        r = client.post(
            "/api/ai/ask",
            json={
                "question": "(confirm)",
                "approval_ref": "tok_abc",
                "tool": "customers.mark_contacted",
                "payload": {"customer_id": "cust-1"},
            },
        )
    assert r.status_code == 200, r.text
    # get_client should NOT have been called — no LLM round-trip.
    assert not mock_get_client.return_value.messages.create.called
