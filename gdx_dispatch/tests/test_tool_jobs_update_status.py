"""Sprint 1.x-S46 — jobs.update_status contract.

Green and preview-only since GDXA-1881; behaviour against real rows lives in
test_mcp_tools_job_stages_soft_delete.py.
"""
from __future__ import annotations

import gdx_dispatch.core.mcp_tools.jobs_update_status  # noqa: F401
from gdx_dispatch.core.mcp_registry import get_tool


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.jobs_update_status import DESCRIPTOR
    assert DESCRIPTOR.name == "jobs.update_status"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "job") in [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert "job_id" in DESCRIPTOR.input_schema.get("required", [])
    assert "new_status" in DESCRIPTOR.input_schema.get("required", [])
    assert "applied" in DESCRIPTOR.output_schema["properties"]
    assert "updated" not in DESCRIPTOR.output_schema["properties"]


def test_tool_registers():
    assert get_tool("jobs.update_status") is not None
