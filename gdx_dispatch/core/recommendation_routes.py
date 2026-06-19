"""gdx_dispatch/core/recommendation_routes.py — FastAPI routes for recommendations and next actions.

Exposes:
  GET  /api/recommendations               — all recommendations for tenant
  GET  /api/recommendations/jobs/{job_id} — job-specific recommendations
  GET  /api/next-actions                  — next-action queue for current user
  POST /api/next-actions/{id}/complete    — mark an action complete
  POST /api/next-actions/{id}/snooze      — snooze an action
  POST /api/next-actions                  — create a manual action
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.core.next_action import queue as action_queue
from gdx_dispatch.core.recommendations import engine as rec_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["recommendations"])

_auth_dep = Depends(require_role("admin", "owner", "dispatcher", "technician", "viewer"))


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class SnoozeRequest(BaseModel):
    until: datetime  # ISO 8601 datetime with timezone


class CreateActionRequest(BaseModel):
    action_type: str
    title: str
    description: str | None = None
    priority: str = "medium"
    action_url: str | None = None
    estimated_value: float = 0.0
    reference_id: str | None = None
    user_id: str | None = None


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
    """Extract user ID from request state; fall back to 'anonymous'."""
    user = getattr(request.state, "user", None) or {}
    return str(user.get("id", "anonymous"))


# ---------------------------------------------------------------------------
# Recommendation routes
# ---------------------------------------------------------------------------

@router.get("/recommendations")
def get_all_recommendations(
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Return all recommendation categories for the current tenant."""
    tenant_id = _get_tenant_id(request)
    return {
        "operational": rec_engine.get_operational_recommendations(
            tenant_id, tenant_db
        ),
        "revenue": rec_engine.get_revenue_recommendations(
            tenant_id, tenant_db
        ),
    }


@router.get("/recommendations/jobs/{job_id}")
def get_job_recommendations(
    job_id: str,
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> list[dict]:
    """Return recommendations specific to a single job."""
    tenant_id = _get_tenant_id(request)
    return rec_engine.get_job_recommendations(tenant_id, job_id, tenant_db)


@router.get("/recommendations/customers/{customer_id}")
def get_customer_recommendations(
    customer_id: str,
    request: Request,
    tenant_db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> list[dict]:
    """Return upsell and follow-up recommendations for a customer."""
    tenant_id = _get_tenant_id(request)
    return rec_engine.get_customer_recommendations(tenant_id, customer_id, tenant_db)


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
    result = action_queue.complete_action(tenant_id, action_id, tenant_db)
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
    result = action_queue.snooze_action(tenant_id, action_id, until_dt, tenant_db)
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
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result
