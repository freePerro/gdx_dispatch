from __future__ import annotations
import time
from fastapi import Request, HTTPException
from fastmcp import FastMCP
from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.unified_principal import Principal
from gdx_dispatch.core.mcp_registry import list_tools_for_principal
from gdx_dispatch.core.mcp_invoke import invoke_tool

mcp = FastMCP(
    name="gdx-mcp",
    instructions="GDX platform MCP — per-tenant tool access via PAT/SCIM auth."
)

_MCP_BUCKET: dict[str, list[float]] = {}
_MCP_RATE_LIMIT_PER_MIN = 60
_MCP_RATE_LIMIT_WINDOW_S = 60

def get_mcp() -> FastMCP:
    """Returns the singleton FastMCP instance."""
    return mcp

async def get_mcp_principal(request: Request) -> Principal:
    """
    FastAPI dependency to resolve the Principal from a Bearer token.
    Delegates to auth_dispatcher.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    try:
        # get_current_principal handles the extraction and validation of the token
        return await get_current_principal(request)
    except HTTPException as e:
        # Re-raise the exception if it's already an HTTPException (e.g. 401)
        # Ensure it includes the WWW-Authenticate header if it's a 401
        if e.status_code == 401:
            e.headers = {"WWW-Authenticate": "Bearer"}
        raise e
    except Exception as exc:
        # For any other unexpected errors during auth, return 401
        raise HTTPException(
            status_code=401,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

def list_tools_for_mcp_principal(principal: Principal) -> list[dict]:
    """
    Returns tool descriptors filtered by principal capability.
    Matches the shape used by the Anthropic loop in gdx_dispatch/routers/ai.py.
    """
    tools = list_tools_for_principal(principal)
    return [
        {
            "name": d.name,
            "description": d.description,
            "input_schema": d.input_schema,
        }
        for d in tools
    ]

def _check_mcp_rate_limit(principal: Principal) -> tuple[bool, int]:
    """
    Checks if the principal has exceeded the MCP rate limit.
    Returns (is_allowed, retry_after_s).
    """
    token_id = getattr(principal, "pat_id", None) or principal.identity_id
    bucket_key = f"{principal.tenant_id}:{token_id}"

    now = time.time()

    # Initialize or clean bucket
    if bucket_key not in _MCP_BUCKET:
        _MCP_BUCKET[bucket_key] = []

    # Remove timestamps older than the window
    _MCP_BUCKET[bucket_key] = [t for t in _MCP_BUCKET[bucket_key] if now - t < _MCP_RATE_LIMIT_WINDOW_S]

    if len(_MCP_BUCKET[bucket_key]) >= _MCP_RATE_LIMIT_PER_MIN:
        # Calculate retry after: time until the oldest timestamp in the window expires
        oldest = _MCP_BUCKET[bucket_key][0]
        retry_after = int(_MCP_RATE_LIMIT_WINDOW_S - (now - oldest)) + 1
        return False, max(retry_after, 1)

    _MCP_BUCKET[bucket_key].append(now)
    return True, 0

def _reset_mcp_rate_limit() -> None:
    """Clears the MCP rate limit bucket. Used for testing."""
    _MCP_BUCKET.clear()

async def call_tool_for_mcp_principal(
    name: str,
    payload: dict,
    *,
    principal: Principal,
    db=None
) -> dict:
    """
    Wraps invoke_tool to provide MCP-compatible responses.
    """
    # Check rate limit first
    allowed, retry_after = _check_mcp_rate_limit(principal)
    if not allowed:
        return {
            "ok": False,
            "error_type": "rate_limit_exceeded",
            "retry_after_s": retry_after,
        }

    # Extract approval_ref from payload if present to pass as a kw-only arg to invoke_tool
    approval_ref = payload.pop("approval_ref", None)

    result = await invoke_tool(name, payload, principal=principal, db=db, approval_ref=approval_ref)

    if result.ok:
        return {"ok": True, "result": result.result}

    if result.error_type == "approval_required":
        return {
            "ok": False,
            "error_type": "approval_required",
            "pending_action": result.error_body.get("result", {}),
        }

    return {
        "ok": False,
        "error_type": result.error_type,
        "detail": result.error_body,
    }
