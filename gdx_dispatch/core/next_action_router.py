"""gdx_dispatch/core/next_action_router.py — FastAPI routes for next-action queue and per-job recommendations.

Provides:
  GET  /api/next-actions                      — list action queue for tenant/user
  POST /api/next-actions                      — create a new action
  POST /api/next-actions/{id}/complete        — mark action completed
  POST /api/next-actions/{id}/snooze          — snooze action until datetime
  GET  /api/recommendations/jobs/{job_id}     — job-specific recommendations
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.next_action import queue as _queue
from gdx_dispatch.core.recommendations import engine as _engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["next-actions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    return str(tenant.get("id", ""))


def _user_id(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    return str(user.get("id", ""))


def _require_tenant(request: Request) -> str:
    tid = _tenant_id(request)
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


# ---------------------------------------------------------------------------
# GET /api/next-actions
# ---------------------------------------------------------------------------

@router.get("/next-actions")
def list_next_actions(
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> list[dict]:
    """Return the prioritised next-action queue for the current tenant and user."""
    tenant_id = _require_tenant(request)
    user_id = _user_id(request)
    try:
        return _queue.get_queue(tenant_id, user_id, tenant_db)
    except Exception as exc:
        logger.error("list_next_actions failed for tenant %s: %s", tenant_id, exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve action queue") from None


# ---------------------------------------------------------------------------
# POST /api/next-actions
# ---------------------------------------------------------------------------

@router.post("/next-actions", status_code=201)
async def create_next_action(
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> dict:
    """Create a new next-action item for the tenant."""
    tenant_id = _require_tenant(request)
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body") from None

    action_type = body.get("type") or body.get("action_type", "")
    title = body.get("title", "")
    if not action_type or not title:
        raise HTTPException(status_code=422, detail="Fields 'type' and 'title' are required")

    result = _queue.create_action(
        tenant_id=tenant_id,
        user_id=body.get("user_id") or _user_id(request) or None,
        action_type=action_type,
        title=title,
        description=body.get("description"),
        priority=body.get("priority", "medium"),
        action_url=body.get("action_url"),
        estimated_value=float(body.get("estimated_value", 0.0)),
        reference_id=body.get("reference_id"),
        tenant_db=tenant_db,
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# POST /api/next-actions/{id}/complete
# ---------------------------------------------------------------------------

@router.post("/next-actions/{action_id}/complete")
def complete_next_action(
    action_id: str,
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> dict:
    """Mark a next-action as completed."""
    tenant_id = _require_tenant(request)
    result = _queue.complete_action(tenant_id, action_id, tenant_db)
    if "error" in result and result.get("error") == "Action not found":
        raise HTTPException(status_code=404, detail="Action not found")
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# POST /api/next-actions/{id}/snooze
# ---------------------------------------------------------------------------

@router.post("/next-actions/{action_id}/snooze")
async def snooze_next_action(
    action_id: str,
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> dict:
    """Snooze a next-action until the specified datetime."""
    tenant_id = _require_tenant(request)
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body") from None

    until_raw = body.get("until")
    if not until_raw:
        raise HTTPException(status_code=422, detail="Field 'until' is required")

    try:
        until_dt = datetime.fromisoformat(str(until_raw))
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Field 'until' must be a valid ISO 8601 datetime string",
        ) from None

    result = _queue.snooze_action(tenant_id, action_id, until_dt, tenant_db)
    if "error" in result and result.get("error") == "Action not found":
        raise HTTPException(status_code=404, detail="Action not found")
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# GET /api/recommendations/jobs/{job_id}
# ---------------------------------------------------------------------------

@router.get("/recommendations/jobs/{job_id}")
def job_recommendations(
    job_id: str,
    request: Request,
    tenant_db: Session = Depends(get_db),
) -> list[dict]:
    """Return rule-based recommendations for a single job."""
    tenant_id = _require_tenant(request)
    try:
        return _engine.get_job_recommendations(tenant_id, job_id, tenant_db)
    except Exception as exc:
        logger.error(
            "job_recommendations failed for tenant %s job %s: %s",
            tenant_id, job_id, exc,
        )
        raise HTTPException(status_code=500, detail="Failed to retrieve job recommendations") from None
