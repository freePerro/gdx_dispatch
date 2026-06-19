"""Sprint 1.x-S16 — multi-tool loop contract."""
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
class _StubAdmin:
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
    app.dependency_overrides[get_current_principal_for_ai] = lambda: _StubAdmin()
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


def test_tools_payload_contains_both_customer_tools(app_and_client):
    """When the loop fires, the `tools=[...]` arg passed to Anthropic must
    include both customers.list AND customers.mark_contacted (the two
    currently-registered tools)."""
    client = app_and_client

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client:
        fake = MagicMock()
        fake.messages.create.return_value = _resp_text("done")
        mock_get_client.return_value = fake

        r = client.post("/api/ai/ask", json={"question": "q"})

    assert r.status_code == 200, r.text
    # Inspect the kwargs passed to messages.create.
    assert fake.messages.create.called
    call_kwargs = fake.messages.create.call_args.kwargs
    tools = call_kwargs.get("tools") or []
    tool_names = sorted(t.get("name") for t in tools)
    assert "customers_list" in tool_names
    assert "customers_mark_contacted" in tool_names


def test_tools_payload_filters_by_principal_capabilities(app_and_client):
    """A principal with ONLY ('read', 'customer') sees customers.list but
    NOT customers.mark_contacted (which requires ('write','customer.contact'))."""

    @dataclass
    class _ReadOnlyAdmin:
        identity_id: Any = field(default_factory=uuid4)
        tenant_id: str = "11111111-1111-1111-1111-111111111111"
        principal_role: str = "viewer"
        capabilities: tuple = (("read", "customer"),)
        auth_kind: str = "session"
        actor_type: str = "human"
        delegated_by_user_id: str | None = None
        is_super_admin: bool = False
        is_restricted: bool = False

    app = FastAPI()
    app.include_router(ai_router)
    app.dependency_overrides[get_current_principal_for_ai] = lambda: _ReadOnlyAdmin()
    app.dependency_overrides[get_db_for_ai] = lambda: MagicMock()
    client = TestClient(app)

    with patch("gdx_dispatch.routers.ai.get_key", return_value="sk-ant-test"), \
         patch("gdx_dispatch.routers.ai.get_client") as mock_get_client:
        fake = MagicMock()
        fake.messages.create.return_value = _resp_text("done")
        mock_get_client.return_value = fake

        r = client.post("/api/ai/ask", json={"question": "q"})

    assert r.status_code == 200, r.text
    call_kwargs = fake.messages.create.call_args.kwargs
    tools = call_kwargs.get("tools") or []
    tool_names = sorted(t.get("name") for t in tools)
    assert "customers_list" in tool_names
    # mark_contacted requires a write cap the AIWorker doesn't inherit:
    assert "customers_mark_contacted" not in tool_names
