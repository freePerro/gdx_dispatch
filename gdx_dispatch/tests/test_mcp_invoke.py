"""Tests for gdx_dispatch.core.mcp_invoke (SS-19 slice B)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from gdx_dispatch.core.mcp_error_schema import (
    ERROR_APPROVAL_REQUIRED,
    ERROR_CAPABILITY_DENIED,
    ERROR_EXECUTION_ERROR,
    ERROR_INPUT_INVALID,
    ERROR_TOOL_NOT_FOUND,
)
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import clear_registry, register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


@dataclass
class Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[dict[str, Any]] = field(default_factory=list)


def _descriptor(name="t.echo", **over) -> ToolDescriptor:
    kw = dict(
        name=name,
        description=f"desc {name}",
        input_schema={
            "type": "object",
            "required": ["msg"],
            "properties": {
                "msg": {"type": "string"},
                "secret": {"type": "string", "sensitive": True},
            },
        },
        output_schema={"type": "object"},
        capabilities_required=[("read", "thing")],
    )
    kw.update(over)
    return ToolDescriptor(**kw)


@pytest.fixture(autouse=True)
def _iso():
    clear_registry()
    yield
    clear_registry()


def _run(coro):
    return asyncio.run(coro)


# ── Resolve ───────────────────────────────────────────────────────────────


def test_unknown_tool_returns_tool_not_found():
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    r = _run(invoke_tool("nope", {"msg": "x"}, principal=p))
    assert not r.ok
    assert r.error_type == ERROR_TOOL_NOT_FOUND
    assert r.error_body["tool"] == "nope"
    assert r.trace_id


# ── Validation precedes capability check ──────────────────────────────────


def test_input_invalid_fires_before_capability_check():
    # Principal lacks capability but also payload is malformed — must
    # get input_invalid so that order-of-operations leak is impossible.
    async def _h(**_):  # pragma: no cover — should not run
        raise AssertionError("handler should not run on invalid input")

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[])  # no caps
    r = _run(invoke_tool("t.echo", {"wrong_field": 1}, principal=p))
    assert not r.ok
    assert r.error_type == ERROR_INPUT_INVALID
    assert any("msg" in e for e in r.error_body["errors"])


def test_input_type_mismatch_caught():
    async def _h(**_):
        return {}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    r = _run(invoke_tool("t.echo", {"msg": 123}, principal=p))
    assert r.error_type == ERROR_INPUT_INVALID


def test_payload_must_be_object():
    async def _h(**_):
        return {}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    r = _run(invoke_tool("t.echo", "not a dict", principal=p))
    assert r.error_type == ERROR_INPUT_INVALID


# ── Capability check ──────────────────────────────────────────────────────


def test_capability_denied_when_principal_lacks_cap():
    async def _h(**_):  # pragma: no cover — should not run
        raise AssertionError("handler should not run")

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "other"}])
    r = _run(invoke_tool("t.echo", {"msg": "hi"}, principal=p))
    assert r.error_type == ERROR_CAPABILITY_DENIED
    assert r.error_body["capabilities_required"] == [["read", "thing"]]
    # Denied call is audited.
    assert r.audit_row["outcome"] == "denied"


# ── Happy path ────────────────────────────────────────────────────────────


def test_happy_path_returns_result_and_allowed_audit():
    async def _h(*, msg, **_):
        return {"echo": msg}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    rows = []
    r = _run(
        invoke_tool(
            "t.echo",
            {"msg": "hi"},
            principal=p,
            log_fn=lambda row: rows.append(row),
        )
    )
    assert r.ok
    assert r.result == {"echo": "hi"}
    assert r.duration_ms >= 0
    assert rows[0]["outcome"] == "allowed"
    assert rows[0]["tool_name"] == "t.echo"


# ── Approval gate ─────────────────────────────────────────────────────────


def test_approval_required_first_call_returns_202_type():
    async def _h(**_):
        return {"rotation_id": "r1", "status": "pending_approval"}

    d = _descriptor(
        name="pat.rotate",
        capabilities_required=[("admin", "pat")],
        sensitivity_class="restricted",
        approval_required=True,
        input_schema={
            "type": "object",
            "required": ["pat_id"],
            "properties": {"pat_id": {"type": "string"}},
        },
    )
    register_tool(d, _h)
    p = Principal(
        capabilities=[{"action": "admin", "resource_type": "pat", "restricted": True}]
    )
    r = _run(invoke_tool("pat.rotate", {"pat_id": "pat-1"}, principal=p))
    assert r.error_type == ERROR_APPROVAL_REQUIRED
    assert r.error_body["status"] == "pending_approval"
    assert r.audit_row["outcome"] == "pending_approval"


def test_approval_ref_lets_second_call_execute():
    async def _h(**_):
        return {"rotated": True}

    d = _descriptor(
        name="pat.rotate",
        capabilities_required=[("admin", "pat")],
        sensitivity_class="restricted",
        approval_required=True,
        input_schema={
            "type": "object",
            "required": ["pat_id"],
            "properties": {"pat_id": {"type": "string"}},
        },
    )
    register_tool(d, _h)
    p = Principal(
        capabilities=[{"action": "admin", "resource_type": "pat", "restricted": True}]
    )
    r = _run(
        invoke_tool(
            "pat.rotate",
            {"pat_id": "pat-1"},
            principal=p,
            approval_ref="approval-xyz",
        )
    )
    assert r.ok
    assert r.result == {"rotated": True}


# ── Execution errors ──────────────────────────────────────────────────────


def test_handler_exception_becomes_execution_error():
    async def _h(**_):
        raise RuntimeError("kaboom")

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    r = _run(invoke_tool("t.echo", {"msg": "hi"}, principal=p))
    assert r.error_type == ERROR_EXECUTION_ERROR
    assert "kaboom" in r.error_body["detail"]
    assert r.audit_row["outcome"] == "error"
    assert "RuntimeError" in r.audit_row["error_detail"]


# ── Redaction ─────────────────────────────────────────────────────────────


def test_sensitive_fields_redacted_in_audit_row():
    async def _h(**_):
        return {"ok": True}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    rows = []
    _run(
        invoke_tool(
            "t.echo",
            {"msg": "hi", "secret": "super-private"},
            principal=p,
            log_fn=lambda r: rows.append(r),
        )
    )
    redacted = rows[0]["input_redacted"]
    assert redacted["msg"] == "hi"
    assert redacted["secret"] == "[REDACTED]"


def test_trace_id_carries_through_when_supplied():
    async def _h(**_):
        return {}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    r = _run(invoke_tool("t.echo", {"msg": "hi"}, principal=p, trace_id="fixed-trace"))
    assert r.trace_id == "fixed-trace"


# ── Nested-object validation (red-team Pattern 4) ─────────────────────────


def test_validate_input_recurses_into_nested_object_with_bad_inner_field():
    from gdx_dispatch.core.mcp_invoke import _validate_input

    schema = {
        "type": "object",
        "required": ["wrap"],
        "properties": {
            "wrap": {
                "type": "object",
                "required": ["inner"],
                "properties": {
                    "inner": {"type": "string"},
                },
            },
        },
    }
    # Inner field has wrong type — should surface as a validation error.
    errors = _validate_input(schema, {"wrap": {"inner": 42}})
    assert errors
    assert any("wrap.inner" in e for e in errors)


def test_validate_input_honors_additional_properties_false():
    from gdx_dispatch.core.mcp_invoke import _validate_input

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
        },
    }
    errors = _validate_input(schema, {"name": "ok", "extra": "nope"})
    assert errors
    assert any("extra" in e for e in errors)


def test_extra_payload_keys_dropped_before_handler_invocation():
    """0.9-s R2: even when schema omits additionalProperties=false (forward
    compat), keys not declared in schema.properties must not reach the
    handler via **kwargs.
    """
    seen_kwargs: dict[str, Any] = {}

    async def handler(*, principal, db, msg: str) -> dict[str, Any]:
        # Deliberately no **kwargs — if extra keys reached here, Python would raise.
        seen_kwargs["msg"] = msg
        return {"got": msg}

    # Schema has only "msg"; no additionalProperties flag (defaults True).
    desc = _descriptor()
    register_tool(desc, handler)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])
    r = _run(
        invoke_tool(
            "t.echo",
            {"msg": "hello", "smuggled_key": "attacker_payload"},
            principal=p,
        )
    )
    assert r.ok, r.error_body
    assert seen_kwargs == {"msg": "hello"}  # smuggled_key filtered out


# ── Real Audit Logging Tests ───────────────────────────────────────────────


def test_invoke_tool_with_db_records_audit_log():
    """Verify that providing a db session triggers log_audit_event_sync."""
    async def _h(*, msg, **_):
        return {"echo": msg}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])

    mock_db = MagicMock(spec=Session)

    # We need to patch log_audit_event_sync where it is used in mcp_invoke
    import gdx_dispatch.core.mcp_invoke
    with MagicMock() as mock_log:
        gdx_dispatch.core.mcp_invoke.log_audit_event_sync = mock_log
        _run(invoke_tool("t.echo", {"msg": "hi"}, principal=p, db=mock_db))

        # Check if log_audit_event_sync was called with correct params
        assert mock_log.called
        args, kwargs = mock_log.call_args
        assert kwargs["action"] == "mcp.tool_invoke"
        assert kwargs["entity_type"] == "mcp_tool"
        assert kwargs["entity_id"] == "t.echo"
        assert kwargs["tenant_id"] == p.tenant_id
        assert kwargs["user_id"] == str(p.identity_id)
        assert "msg" in kwargs["details"]["input_redacted"]


def test_invoke_tool_without_db_logs_debug_only(caplog):
    """Verify that without db, it falls back to debug logging."""
    async def _h(*, msg, **_):
        return {"echo": msg}

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])

    with caplog.at_level("DEBUG"):
        _run(invoke_tool("t.echo", {"msg": "hi"}, principal=p, db=None))

    assert "mcp.invoke.audit" in caplog.text


def test_invoke_tool_error_records_audit_log_with_details():
    """Verify that handler exceptions are recorded in the audit log."""
    async def _h(**_):
        raise RuntimeError("kaboom")

    register_tool(_descriptor(), _h)
    p = Principal(capabilities=[{"action": "read", "resource_type": "thing"}])

    mock_db = MagicMock(spec=Session)
    import gdx_dispatch.core.mcp_invoke
    with MagicMock() as mock_log:
        gdx_dispatch.core.mcp_invoke.log_audit_event_sync = mock_log
        _run(invoke_tool("t.echo", {"msg": "hi"}, principal=p, db=mock_db))

        assert mock_log.called
        args, kwargs = mock_log.call_args
        # The outcome lives inside details (the audit-event-sync API takes
        # action/tenant_id/user_id/entity_type/entity_id/details, not outcome).
        assert kwargs["details"]["outcome"] == "error"
        assert "RuntimeError: kaboom" in kwargs["details"]["error_detail"]
