"""SS-19 slice A — canonical MCP error bodies.

Every error the MCP execute/SSE surfaces returns goes through this
module so client-side error handling is deterministic. The body shape
is identical across routers:

    {
        "error_type": "<slug>",
        "detail": "<human-readable>",
        "trace_id": "<uuid>",
        ...extras
    }

Why a dedicated module
----------------------
* The MCP spec (Anthropic 2025-11) expects structured errors that
  clients can discriminate on by machine-readable slug — we do NOT
  leak raw FastAPI/HTTPException shapes.
* Keeping the slugs in one file makes drift impossible; every surface
  imports from here.
* "Silent failure is not failure, it is lying": every error carries a
  ``trace_id`` so an operator can trace a specific MCP call end-to-end
  through logs + audit rows.

Slugs
-----
* ``input_invalid``      — 400 — payload failed JSON Schema validation
* ``tool_not_found``     — 404 — no tool registered under that name
* ``capability_denied``  — 403 — caller lacks a required capability
* ``approval_required``  — 202 — approval-gated tool, staged pending
* ``execution_error``    — 500 — handler raised an unexpected exception

No silent failures — ``approval_required`` returns 202 (not a true
error) but shares the same structured envelope so clients handle the
deferred-result case with the same code path.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status


# Slug constants — keep in sync with docs/mcp-quickstart.md and tests.
ERROR_INPUT_INVALID = "input_invalid"
ERROR_TOOL_NOT_FOUND = "tool_not_found"
ERROR_CAPABILITY_DENIED = "capability_denied"
ERROR_APPROVAL_REQUIRED = "approval_required"
ERROR_EXECUTION_ERROR = "execution_error"


VALID_ERROR_TYPES: frozenset[str] = frozenset(
    {
        ERROR_INPUT_INVALID,
        ERROR_TOOL_NOT_FOUND,
        ERROR_CAPABILITY_DENIED,
        ERROR_APPROVAL_REQUIRED,
        ERROR_EXECUTION_ERROR,
    }
)


# Canonical HTTP status for each error_type. Approval-required is 202
# because the request *succeeded* (it was staged) but the result is
# deferred.
_STATUS_FOR: dict[str, int] = {
    ERROR_INPUT_INVALID: status.HTTP_400_BAD_REQUEST,
    ERROR_TOOL_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ERROR_CAPABILITY_DENIED: status.HTTP_403_FORBIDDEN,
    ERROR_APPROVAL_REQUIRED: status.HTTP_202_ACCEPTED,
    ERROR_EXECUTION_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def new_trace_id() -> str:
    """Generate a fresh trace_id — UUID4, string form."""
    return str(uuid4())


def build_error(
    error_type: str,
    detail: str,
    /,
    *,
    trace_id: str | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Assemble a canonical error body.

    Raises
    ------
    ValueError
        If ``error_type`` is not one of the approved slugs. We fail
        loud here because silently accepting unknown slugs would
        degrade client-side dispatch.
    """
    if error_type not in VALID_ERROR_TYPES:
        raise ValueError(
            f"unknown MCP error_type {error_type!r}; "
            f"valid: {sorted(VALID_ERROR_TYPES)!r}"
        )
    body: dict[str, Any] = {
        "error_type": error_type,
        "detail": detail,
        "trace_id": trace_id or new_trace_id(),
    }
    # Extras must not shadow the canonical keys.
    for k, v in extras.items():
        if k in ("error_type", "detail", "trace_id"):
            raise ValueError(f"extras cannot override canonical key {k!r}")
        body[k] = v
    return body


def status_for(error_type: str) -> int:
    """Return the canonical HTTP status for a given error slug."""
    if error_type not in _STATUS_FOR:
        raise ValueError(f"unknown MCP error_type {error_type!r}")
    return _STATUS_FOR[error_type]


def raise_mcp_error(
    error_type: str,
    detail: str,
    *,
    trace_id: str | None = None,
    **extras: Any,
) -> None:
    """Raise an ``HTTPException`` carrying a canonical MCP error body.

    FastAPI serialises ``detail`` as the top-level response body when it
    is a dict, which is exactly what MCP clients expect.
    """
    body = build_error(error_type, detail, trace_id=trace_id, **extras)
    raise HTTPException(status_code=status_for(error_type), detail=body)
