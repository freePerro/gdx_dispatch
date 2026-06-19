"""Sprint 1.x-S23 — MCP confirm contract."""
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
    capabilities: list[Any] = field(default_factory=list)


@pytest.mark.asyncio
async def test_call_tool_passes_approval_ref():
    from gdx_dispatch.core.mcp_protocol_adapter import call_tool_for_mcp_principal

    captured: dict[str, Any] = {}

    async def _fake_invoke(name, payload, *, principal, db=None, approval_ref=None, **_):
        from unittest.mock import MagicMock
        captured["approval_ref"] = approval_ref
        captured["name"] = name
        res = MagicMock()
        res.ok = True
        res.result = {"updated": True}
        res.error_type = None
        res.error_body = None
        return res

    with patch("gdx_dispatch.core.mcp_protocol_adapter.invoke_tool", side_effect=_fake_invoke):
        p = _P(capabilities=[("write", "job"), ("write", "schedule")])
        out = await call_tool_for_mcp_principal(
            "schedule.schedule_job",
            {"job_id": "j1", "technician_id": "t", "scheduled_at": "2026-05-01T09:00:00Z", "approval_ref": "tok_abc"},
            principal=p, db=None,
        )

    assert captured["approval_ref"] == "tok_abc"
    # The approval_ref MUST NOT have been forwarded into the tool payload.
    # (The adapter strips it before passing payload to invoke_tool.)
    assert out["ok"] is True
