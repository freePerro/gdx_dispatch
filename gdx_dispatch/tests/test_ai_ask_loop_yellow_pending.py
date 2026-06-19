"""Sprint 1.x-S17 — Yellow pending_action surfaces to caller."""
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
    capabilities: tuple = (
        ("read", "customer"),
        ("write", "customer.contact"),
    )
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
    return TestClient(app)


def _resp_tool_use(name: str, args: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = args
    block.id = "tu_yellow"
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


def test_yellow_first_call_surfaces_pending_action(app_and_client):
    """Loop dispatches a Yellow tool, invoke_tool returns approval_required;
    the loop must NOT feed the failure back to Claude — it returns
    {pending_action: {...}} to the caller and stops."""
    client = app_and_client

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client, \
         patch("gdx_dispatch.routers.ai.invoke_tool") as mock_invoke:

        fake = MagicMock()
        fake.messages.create.return_value = _resp_tool_use(
            "customers.mark_contacted", {"customer_id": "abc-123"}
        )
        mock_get_client.return_value = fake

        async def _fake_invoke(*args, **kwargs):
            res = MagicMock()
            res.ok = False
            res.error_type = "approval_required"
            res.error_body = {
                "error_type": "approval_required",
                "tool": "customers.mark_contacted",
                "status": "pending_approval",
                "trace_id": "trace-yellow-1",
                "result": {"approval_token": "tok_abc"},
            }
            res.result = None
            return res
        mock_invoke.side_effect = _fake_invoke

        r = client.post("/api/ai/ask", json={"question": "mark customer abc-123 as contacted"})

    assert r.status_code == 200, r.text
    body = r.json()
    # The loop must surface pending_action; it must NOT have called Claude
    # a second time (that would mean it fed the failure back as a tool result).
    assert body.get("pending_action") is not None
    pa = body["pending_action"]
    assert pa.get("tool") == "customers.mark_contacted"
    # The payload Claude requested should be visible so the UI can render it.
    assert pa.get("payload") == {"customer_id": "abc-123"}
    # Token / trace surfaced for S18's confirm flow.
    assert pa.get("approval_token") or pa.get("trace_id")
    # answer is null when pending_action is set; tools_used records the attempt.
    assert body.get("answer") in (None, "")
    assert "customers.mark_contacted" in body.get("tools_used", [])
    # Claude was called exactly once — no second round-trip with a tool_result.
    assert fake.messages.create.call_count == 1


def test_green_tool_success_does_not_set_pending_action(app_and_client):
    """A successful Green tool call must NOT set pending_action."""
    client = app_and_client

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client, \
         patch("gdx_dispatch.routers.ai.invoke_tool") as mock_invoke:

        # Two-step: tool_use, then text answer.
        fake = MagicMock()
        block_text = MagicMock()
        block_text.type = "text"
        block_text.text = "Listed."
        text_resp = MagicMock()
        text_resp.stop_reason = "end_turn"
        text_resp.content = [block_text]
        fake.messages.create.side_effect = [
            _resp_tool_use("customers.list", {}),
            text_resp,
        ]
        mock_get_client.return_value = fake

        async def _fake_invoke(*args, **kwargs):
            res = MagicMock()
            res.ok = True
            res.result = {"customers": []}
            res.error_type = None
            res.error_body = None
            return res
        mock_invoke.side_effect = _fake_invoke

        r = client.post("/api/ai/ask", json={"question": "list"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("pending_action") is None
    assert body["answer"] == "Listed."
