"""Sprint 1.x-S15 — single-tool Haiku loop contract."""
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
class _StubAdmin:
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
    app = FastAPI()
    app.include_router(ai_router)
    admin = _StubAdmin()
    app.dependency_overrides[get_current_principal_for_ai] = lambda: admin
    app.dependency_overrides[get_db_for_ai] = lambda: MagicMock()
    app.dependency_overrides[get_db_for_ai] = lambda: MagicMock()
    return TestClient(app), admin


def _fake_anthropic_response_text_only(text: str):
    """Build a faux Anthropic response object with a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    return resp


def _fake_anthropic_response_tool_use(tool_name: str, tool_input: dict, tool_use_id: str = "tu_1"):
    """Build a faux Anthropic response object with a tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    block.id = tool_use_id
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


def test_loop_text_only_response_returns_answer(app_and_client):
    """When Claude responds with text (no tool_use), the loop terminates and
    returns the text as `answer`."""
    client, admin = app_and_client

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client:
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_anthropic_response_text_only("3 customers found.")
        mock_get_client.return_value = fake_client

        r = client.post("/api/ai/ask", json={"question": "list customers"})

    assert r.status_code == 200
    body = r.json()
    assert body.get("disabled") is not True
    assert body["answer"] == "3 customers found."
    assert body["tools_used"] == []


def test_loop_invokes_tool_then_returns_answer(app_and_client):
    """When Claude requests a tool, the loop invokes it, feeds the result
    back, and returns Claude's final text answer plus the tool name."""
    client, admin = app_and_client

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client, \
         patch("gdx_dispatch.routers.ai.invoke_tool") as mock_invoke_tool:

        fake_client = MagicMock()
        # Two-step conversation: tool_use, then text answer.
        fake_client.messages.create.side_effect = [
            _fake_anthropic_response_tool_use("customers.list", {}),
            _fake_anthropic_response_text_only("Found 2 customers."),
        ]
        mock_get_client.return_value = fake_client

        # invoke_tool is async; mock to return a successful InvocationResult-shaped object.
        async def _fake_invoke(*args, **kwargs):
            res = MagicMock()
            res.ok = True
            res.result = {"customers": [{"id": "c1", "name": "Alice"}, {"id": "c2", "name": "Bob"}]}
            res.error_type = None
            res.error_body = None
            return res
        mock_invoke_tool.side_effect = _fake_invoke

        r = client.post("/api/ai/ask", json={"question": "how many customers do we have?"})

    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Found 2 customers."
    assert "customers.list" in body["tools_used"]


def test_loop_safety_cap_kicks_in(app_and_client):
    """If Claude keeps requesting tools without ever returning text, the loop
    must terminate at the 5-iteration safety cap with a structured response."""
    client, admin = app_and_client

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client, \
         patch("gdx_dispatch.routers.ai.invoke_tool") as mock_invoke_tool:

        fake_client = MagicMock()
        # Always return tool_use — never end_turn. Loop must self-terminate.
        fake_client.messages.create.return_value = _fake_anthropic_response_tool_use(
            "customers.list", {}
        )
        mock_get_client.return_value = fake_client

        async def _fake_invoke(*args, **kwargs):
            res = MagicMock()
            res.ok = True
            res.result = {"customers": []}
            res.error_type = None
            res.error_body = None
            return res
        mock_invoke_tool.side_effect = _fake_invoke

        r = client.post("/api/ai/ask", json={"question": "loop forever"})

    assert r.status_code == 200
    body = r.json()
    # The answer should mention the cap was hit; the exact wording is up to
    # the implementer. Tools used should include customers.list at least once.
    assert "customers.list" in body["tools_used"]
    assert body.get("answer") is not None
    # Never-terminate-loop should NOT mint more than ~5 tool calls.
    assert fake_client.messages.create.call_count <= 6  # 5 tool turns + 1 final attempt


def test_disabled_when_no_key_still_works(app_and_client):
    """The S14 disabled path still fires (didn't get clobbered by S15 wiring)."""
    client, _ = app_and_client
    with patch("gdx_dispatch.routers.ai.get_key", return_value=None):
        r = client.post("/api/ai/ask", json={"question": "anything"})
    assert r.status_code == 200
    body = r.json()
    assert body["disabled"] is True
    assert body["reason"] == "no_key"
