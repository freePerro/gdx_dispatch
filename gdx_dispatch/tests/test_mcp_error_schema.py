"""Tests for gdx_dispatch.core.mcp_error_schema (SS-19 slice A)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from gdx_dispatch.core.mcp_error_schema import (
    ERROR_APPROVAL_REQUIRED,
    ERROR_CAPABILITY_DENIED,
    ERROR_EXECUTION_ERROR,
    ERROR_INPUT_INVALID,
    ERROR_TOOL_NOT_FOUND,
    VALID_ERROR_TYPES,
    build_error,
    new_trace_id,
    raise_mcp_error,
    status_for,
)


def test_every_slug_has_a_canonical_status():
    for slug in VALID_ERROR_TYPES:
        # Every slug must have a status — no silent gaps.
        assert status_for(slug) in {202, 400, 403, 404, 500}


def test_approval_required_is_202_not_error_code():
    # Approval-required is a deferred success, not a failure.
    assert status_for(ERROR_APPROVAL_REQUIRED) == 202


def test_status_map_matches_semantics():
    assert status_for(ERROR_INPUT_INVALID) == 400
    assert status_for(ERROR_CAPABILITY_DENIED) == 403
    assert status_for(ERROR_TOOL_NOT_FOUND) == 404
    assert status_for(ERROR_EXECUTION_ERROR) == 500


def test_build_error_has_mandatory_fields():
    body = build_error(ERROR_INPUT_INVALID, "bad shape")
    assert body["error_type"] == ERROR_INPUT_INVALID
    assert body["detail"] == "bad shape"
    assert body["trace_id"]  # uuid-shaped string
    assert len(body["trace_id"]) >= 32


def test_build_error_trace_id_can_be_supplied():
    body = build_error(ERROR_INPUT_INVALID, "x", trace_id="abc123")
    assert body["trace_id"] == "abc123"


def test_build_error_rejects_unknown_slug():
    with pytest.raises(ValueError):
        build_error("oops_not_a_slug", "x")


def test_build_error_rejects_canonical_key_override():
    with pytest.raises(ValueError):
        build_error(ERROR_INPUT_INVALID, "x", **{"error_type": "other"})


def test_build_error_allows_extras():
    body = build_error(
        ERROR_CAPABILITY_DENIED,
        "missing read:customer",
        tool="customer.lookup",
        capabilities_required=[["read", "customer"]],
    )
    assert body["tool"] == "customer.lookup"
    assert body["capabilities_required"] == [["read", "customer"]]


def test_raise_mcp_error_raises_httpexception_with_canonical_body():
    with pytest.raises(HTTPException) as ei:
        raise_mcp_error(ERROR_TOOL_NOT_FOUND, "nope", tool="x.y")
    assert ei.value.status_code == 404
    assert ei.value.detail["error_type"] == ERROR_TOOL_NOT_FOUND
    assert ei.value.detail["tool"] == "x.y"
    assert ei.value.detail["trace_id"]


def test_new_trace_id_uniqueness():
    a, b = new_trace_id(), new_trace_id()
    assert a != b
