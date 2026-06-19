"""SS-25 Slice D — tests for sandbox_context helpers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from gdx_dispatch.core.sandbox_context import (
    SandboxOperationBlocked,
    assert_not_sandbox,
    is_sandbox,
)


def _mk_request(headers: dict[str, str] | None = None, tenant=None) -> Request:
    """Build a minimal Starlette Request for the helpers."""
    header_list = []
    for k, v in (headers or {}).items():
        header_list.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": header_list,
        "query_string": b"",
    }
    req = Request(scope)
    if tenant is not None:
        req.state.tenant = tenant
    return req


def test_no_signals_means_production():
    assert is_sandbox(_mk_request()) is False


def test_header_1_wins():
    assert is_sandbox(_mk_request({"X-GDX-Sandbox": "1"})) is True


def test_header_true_caseinsensitive():
    assert is_sandbox(_mk_request({"X-GDX-Sandbox": "TRUE"})) is True
    assert is_sandbox(_mk_request({"X-GDX-Sandbox": "Yes"})) is True


def test_header_falsy_values_are_production():
    assert is_sandbox(_mk_request({"X-GDX-Sandbox": "0"})) is False
    assert is_sandbox(_mk_request({"X-GDX-Sandbox": "false"})) is False
    assert is_sandbox(_mk_request({"X-GDX-Sandbox": ""})) is False


def test_tenant_dict_flag_wins():
    req = _mk_request(tenant={"id": "t1", "is_sandbox": True})
    assert is_sandbox(req) is True


def test_tenant_object_flag_wins():
    req = _mk_request(tenant=SimpleNamespace(id="t1", is_sandbox=True))
    assert is_sandbox(req) is True


def test_tenant_without_flag_is_production():
    req = _mk_request(tenant={"id": "t1"})
    assert is_sandbox(req) is False


def test_signals_compose_or_not_and():
    # tenant production but header says sandbox → sandbox wins
    req = _mk_request(
        headers={"X-GDX-Sandbox": "1"},
        tenant={"id": "t1", "is_sandbox": False},
    )
    assert is_sandbox(req) is True


def test_assert_not_sandbox_passes_in_production():
    assert_not_sandbox(_mk_request(), operation="charge_card")


def test_assert_not_sandbox_blocks_with_op_name():
    req = _mk_request({"X-GDX-Sandbox": "1"})
    with pytest.raises(SandboxOperationBlocked) as exc:
        assert_not_sandbox(req, operation="charge_card")
    assert "charge_card" in str(exc.value)
