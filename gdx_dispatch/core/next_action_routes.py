"""gdx_dispatch/core/next_action_routes.py — FastAPI routes for next actions.

Exposes:
  GET  /api/next-actions                  — next-action queue for current user
  POST /api/next-actions/{id}/complete    — mark an action complete
  POST /api/next-actions/{id}/snooze      — snooze an action
  POST /api/next-actions                  — create a manual action
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import resolve_audit_actor
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.core.next_action import queue as action_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["next-actions"])

_auth_dep = Depends(require_role("admin", "owner", "dispatcher", "technician", "viewer"))


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class SnoozeRequest(BaseModel):
    until: datetime  # ISO 8601 datetime with timezone


class CreateActionRequest(BaseModel):
    """Body of POST /api/next-actions.

    Every bound mirrors the ``NextAction`` column it lands in
    (``core/next_action.py``). Before GDXA-1808 none was set: an over-long
    value failed the commit on Postgres, ``create_action`` swallowed it, and
    the caller got a bare 500 instead of a 422 naming the field.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    priority: str = Field(default="medium", max_length=10)
    action_url: str | None = Field(default=None, max_length=500)
    estimated_value: float = Field(default=0.0, allow_inf_nan=False)
    reference_id: str | None = Field(default=None, max_length=100)
    # Accepted for old clients and ignored: the route always assigns the
    # authenticated caller (see create_next_action).
    user_id: str | None = Field(default=None, max_length=100)

    @field_validator(
        "action_type", "title", "description", "priority", "action_url", "reference_id", "user_id",
    )
    @classmethod
    def _no_nul(cls, v: str | None) -> str | None:
        """Postgres text cannot hold NUL; psycopg2 refuses it at flush and the
        caller got the same bare 500 as an over-long value."""
        if v is not None and "\x00" in v:
            raise ValueError("must not contain NUL characters")
        return v

    @field_validator("action_url")
    @classmethod
    def _same_origin_path(cls, v: str | None) -> str | None:
        """The dashboard hands this to ``router.push``, so it must be an
        in-app path: one leading ``/``, no ``//host``, no scheme, no
        backslash (browsers read ``/\\host`` as ``//host``), no whitespace or
        control characters."""
        if v is None:
            return v
        if (
            not v.startswith("/")
            or v.startswith("//")
            or "\\" in v
            or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in v)
        ):
            raise ValueError("action_url must be an in-app path starting with a single '/'")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tid = str(tenant.get("id", ""))
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _get_user_id(request: Request) -> str:
    """The caller's user id; 'anonymous' only when there is no principal.

    Read ``request.state.user["id"]`` before — the login dict carries
    ``user_id``, never ``id``, and these routes gate on ``require_role``, which
    stashes its principal on ``state.current_user`` instead. Every caller was
    "anonymous": a manual next-action named nobody, and one assigned to a real
    user would have been hidden from everyone's queue (#701).
    """
    actor = resolve_audit_actor(None, request)
    return "anonymous" if actor == "system" else actor


# ---------------------------------------------------------------------------
# Next-action routes
# ---------------------------------------------------------------------------

@router.get("/next-actions")
def get_next_actions(
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> list[dict]:
    """Return the prioritised next-action queue for the current user."""
    tenant_id = _get_tenant_id(request)
    user_id = _get_user_id(request)
    return action_queue.get_queue(tenant_id, user_id, tenant_db)


@router.post("/next-actions/{action_id}/complete")
def complete_next_action(
    action_id: str,
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Mark a next action as completed."""
    tenant_id = _get_tenant_id(request)
    result = action_queue.complete_action(tenant_id, action_id, tenant_db, request=request)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/next-actions/{action_id}/snooze")
def snooze_next_action(
    action_id: str,
    body: SnoozeRequest,
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Snooze a next action until a specified future datetime."""
    tenant_id = _get_tenant_id(request)
    until_dt = body.until
    # Ensure timezone-aware
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)
    result = action_queue.snooze_action(tenant_id, action_id, until_dt, tenant_db, request=request)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/next-actions")
def create_next_action(
    body: CreateActionRequest,
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Create a new manual next action for the current tenant."""
    tenant_id = _get_tenant_id(request)
    # Use the authenticated user's id, never body.user_id. Trusting the body
    # would let user A assign actions to user B within the same tenant
    # (intra-tenant impersonation in audit/assignment records).
    auth_user_id = _get_user_id(request)
    result = action_queue.create_action(
        tenant_id=tenant_id,
        user_id=auth_user_id or body.user_id,
        action_type=body.action_type,
        title=body.title,
        description=body.description,
        priority=body.priority,
        action_url=body.action_url,
        estimated_value=body.estimated_value,
        reference_id=body.reference_id,
        tenant_db=tenant_db,
        request=request,
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result
