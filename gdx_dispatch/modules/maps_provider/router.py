"""Maps provider config — GET/PATCH per-tenant."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_ready_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.settings_audit import audited_settings_upsert
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maps", tags=["maps"])


_VALID = {"google_maps", "mapbox", "osm"}
_WIRED_TODAY = {"google_maps"}


class ProviderIn(BaseModel):
    provider: str


def _tid(request: Request) -> UUID:
    raw = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


@router.get("/provider", response_model=None)
def get_provider(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    cdb: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    row = cdb.execute(
        text("SELECT maps_provider FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": str(_tid(request))},
    ).first()
    return {
        "provider": (row[0] if row else "google_maps") or "google_maps",
        "candidates": sorted(_VALID),
        "wired_today": sorted(_WIRED_TODAY),
    }


@router.patch("/provider", response_model=None)
def set_provider(
    payload: ProviderIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    cdb: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    if payload.provider not in _VALID:
        raise HTTPException(status_code=422, detail=f"invalid provider '{payload.provider}'")
    if payload.provider not in _WIRED_TODAY:
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.provider}' is planned; only {sorted(_WIRED_TODAY)} are wired today.",
        )
    tid = str(_tid(request))
    # Invariant #1. `_read` here is the GET's own projection, so the audit
    # diff records exactly what a reader would see change.
    audited_settings_upsert(
        cdb,
        request,
        user,
        tenant_id=tid,
        values={"maps_provider": payload.provider},
        action="maps_provider_updated",
        read=lambda db, t: {
            "maps_provider": (
                db.execute(
                    text("SELECT maps_provider FROM tenant_settings WHERE tenant_id = :tid"),
                    {"tid": str(t)},
                ).scalar()
            )
        },
    )
    return get_provider(request, user, cdb)
