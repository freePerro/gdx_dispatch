"""
Feature Flags admin routes — superadmin-only management UI and API.

Routes
------
GET  /admin/feature-flags                   — HTML dashboard (superadmin)
GET  /api/admin/feature-flags               — list all flags (superadmin)
POST /api/admin/feature-flags               — create a new flag (superadmin)
PATCH /api/admin/feature-flags/{key}        — update rollout % (superadmin)
POST /api/admin/feature-flags/{key}/overrides          — set tenant override (superadmin)
DELETE /api/admin/feature-flags/{key}/overrides/{tid}  — remove tenant override (superadmin)
GET  /api/feature-flags                     — tenant's own flag states (tenant auth via X-Tenant-ID header)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import PlatformFeatureFlag as FeatureFlag
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.feature_flags import (
    create_flag,
    delete_tenant_override,
    get_flag_stats,
    get_flags_for_tenant,
    list_flags,
    set_rollout_percentage,
    set_tenant_override,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)
ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")


def _require_superadmin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Bearer token guard for superadmin endpoints."""
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
    if credentials is None or credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access denied",
        )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class FlagCreateBody(BaseModel):
    flag_key: str = Field(..., pattern=r"^[a-z0-9_]+$", description="lowercase + underscores")
    description: str = Field(default="")
    default_value: bool = Field(default=False)
    rollout_pct: int = Field(default=0, ge=0, le=100)


class FlagPatchBody(BaseModel):
    rollout_pct: int = Field(..., ge=0, le=100)


class OverrideBody(BaseModel):
    tenant_id: str
    value: bool


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_TMPL = Path(__file__).parent.parent / "templates" / "feature_flags.html"


@router.get(
    "/admin/feature-flags",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_superadmin)],
)
async def feature_flags_dashboard() -> HTMLResponse:
    """Serve the feature flags management UI."""
    if _TMPL.exists():
        return HTMLResponse(content=_TMPL.read_text())
    return HTMLResponse(content="<h1>Feature flags template not found</h1>", status_code=200)


# ---------------------------------------------------------------------------
# Admin API — superadmin only
# ---------------------------------------------------------------------------


@router.get(
    "/api/admin/feature-flags",
    dependencies=[Depends(_require_superadmin)],
    tags=["feature-flags"],
)
def api_list_flags(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Return all feature flags with rollout %, override counts, and per-flag stats."""
    flags = list_flags(db)
    for f in flags:
        stats = get_flag_stats(f["flag_key"], db)
        f["enabled_overrides"] = stats["enabled_overrides"]
        f["disabled_overrides"] = stats["disabled_overrides"]
    return flags


@router.post(
    "/api/admin/feature-flags",
    dependencies=[Depends(_require_superadmin)],
    status_code=status.HTTP_201_CREATED,
    tags=["feature-flags"],
)
def api_create_flag(
    body: FlagCreateBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Register a new feature flag."""
    try:
        flag = create_flag(
            key=body.flag_key,
            description=body.description,
            default_value=body.default_value,
            rollout_percent=body.rollout_pct,
            db=db,
        )
        return {"status": "created", "flag_key": flag.flag_key}
    except ValueError as exc:
        # Duplicate key → 409 Conflict
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch(
    "/api/admin/feature-flags/{flag_key}",
    dependencies=[Depends(_require_superadmin)],
    tags=["feature-flags"],
)
def api_update_flag(
    flag_key: str,
    body: FlagPatchBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update the rollout percentage for a flag."""
    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found")
    try:
        set_rollout_percentage(flag_key, body.rollout_pct, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"status": "updated", "flag_key": flag_key, "rollout_pct": body.rollout_pct}


@router.post(
    "/api/admin/feature-flags/{flag_key}/overrides",
    dependencies=[Depends(_require_superadmin)],
    status_code=status.HTTP_201_CREATED,
    tags=["feature-flags"],
)
def api_add_override(
    flag_key: str,
    body: OverrideBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Force-enable or force-disable a flag for a single tenant."""
    set_tenant_override(flag_key, body.tenant_id, body.value, db)
    return {
        "status": "override_set",
        "flag_key": flag_key,
        "tenant_id": body.tenant_id,
        "value": body.value,
    }


@router.delete(
    "/api/admin/feature-flags/{flag_key}/overrides/{tenant_id}",
    dependencies=[Depends(_require_superadmin)],
    tags=["feature-flags"],
)
def api_remove_override(
    flag_key: str,
    tenant_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Remove a tenant-specific override; tenant falls back to rollout %."""
    delete_tenant_override(tenant_id, flag_key, db)
    return {"status": "override_removed", "flag_key": flag_key, "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# Tenant-facing API — returns the calling tenant's flag states
# ---------------------------------------------------------------------------


@router.get(
    "/api/feature-flags",
    tags=["feature-flags"],
)
def api_tenant_flags(
    request: Request,
    tenant_id: str | None = Query(default=None, description="Tenant UUID (falls back to X-Tenant-ID header)"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return flag states for the calling tenant.

    The tenant is identified by the ``tenant_id`` query param or the
    ``X-Tenant-ID`` request header (set by TenantMiddleware).
    """
    tid = tenant_id or request.headers.get("X-Tenant-ID", "")
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required (query param or X-Tenant-ID header)",
        )

    flags_map = get_flags_for_tenant(tid, db)
    all_flags = list_flags(db)

    result = []
    for f in all_flags:
        overrides = f.get("tenant_overrides") or {}
        override_val = overrides.get(tid)
        result.append({
            "flag_key": f["flag_key"],
            "rollout_pct": f["rollout_pct"],
            "override_for_this_tenant": override_val,
            "enabled_for_this_tenant": flags_map.get(f["flag_key"], False),
        })
    return result
