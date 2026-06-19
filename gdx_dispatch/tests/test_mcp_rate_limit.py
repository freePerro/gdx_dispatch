"""Sprint 1.x-S24 — MCP rate limit."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from uuid import uuid4
import pytest


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    pat_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=lambda: [("read", "customer")])


@pytest.fixture(autouse=True)
def _reset():
    import gdx_dispatch.core.mcp_protocol_adapter as mod
    if hasattr(mod, "_reset_mcp_rate_limit"):
        mod._reset_mcp_rate_limit()
    yield
    if hasattr(mod, "_reset_mcp_rate_limit"):
        mod._reset_mcp_rate_limit()


@pytest.mark.asyncio
async def test_61st_call_returns_rate_limit_error():
    from gdx_dispatch.core.mcp_protocol_adapter import call_tool_for_mcp_principal

    async def _fake_invoke(*_, **__):
        from unittest.mock import MagicMock
        res = MagicMock()
        res.ok = True
        res.result = {}
        res.error_type = None
        res.error_body = None
        return res

    with patch("gdx_dispatch.core.mcp_protocol_adapter.invoke_tool", side_effect=_fake_invoke):
        p = _P()
        for i in range(60):
            out = await call_tool_for_mcp_principal("customers.list", {}, principal=p, db=None)
            assert out.get("ok") is True, f"call {i} should pass"
        # 61st must hit rate limit
        out = await call_tool_for_mcp_principal("customers.list", {}, principal=p, db=None)
    assert out.get("ok") is False
    assert "rate" in (out.get("error_type") or "").lower() or "limit" in (out.get("error_type") or "").lower()
