"""Holding Areas — custom job queues (Needs Parts, Waiting, etc.)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import HoldingArea
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/holding-areas",
    tags=["holding-areas"],
    dependencies=[Depends(require_module("jobs"))],
)

DEFAULT_AREAS = [
    {"name": "Needs Parts", "color": "#f59e0b"},
    {"name": "Waiting on Customer", "color": "#8b5cf6"},
    {"name": "Ready to Schedule", "color": "#10b981"},
]


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


class HoldingAreaIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(default="#6b7280", max_length=20)


@router.get("")
def list_areas(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = (
        db.query(HoldingArea)
        .filter(HoldingArea.deleted_at.is_(None))
        .order_by(HoldingArea.sort_order, HoldingArea.name)
        .all()
    )
    return [{"id": r.id, "name": r.name, "color": r.color, "sort_order": r.sort_order} for r in rows]


@router.post("", status_code=201)
def create_area(
    request: Request, payload: HoldingAreaIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    area_id = str(uuid4())
    now = datetime.now(timezone.utc)
    area = HoldingArea(
        id=area_id,
        company_id=tid,
        name=payload.name,
        color=payload.color,
        sort_order=0,
        created_at=now,
    )
    db.add(area)
    db.commit()
    log_audit_event_sync(db, tenant_id=tid, user_id=_uid(user), action="create",
                         entity_type="holding_area", entity_id=area_id,
                         details={"name": payload.name}, request=request)
    return {"id": area_id, "name": payload.name, "color": payload.color}


@router.put("/{area_id}")
def update_area(
    area_id: str, request: Request, payload: HoldingAreaIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    area = (
        db.query(HoldingArea)
        .filter(HoldingArea.id == area_id, HoldingArea.deleted_at.is_(None))
        .first()
    )
    if area:
        area.name = payload.name
        area.color = payload.color
        db.commit()
    log_audit_event_sync(db, tenant_id=tid, user_id=_uid(user), action="update",
                         entity_type="holding_area", entity_id=area_id,
                         details={"name": payload.name}, request=request)
    return {"status": "updated", "id": area_id}


@router.delete("/{area_id}")
def delete_area(
    area_id: str, request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    now = datetime.now(timezone.utc)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    area = (
        db.query(HoldingArea)
        .filter(HoldingArea.id == area_id)
        .first()
    )
    if area:
        area.deleted_at = now
        db.commit()
    log_audit_event_sync(db, tenant_id=tid, user_id=_uid(user), action="delete",
                         entity_type="holding_area", entity_id=area_id, details={}, request=request)
    return {"status": "deleted"}


@router.post("/seed-defaults")
def seed_defaults(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Seed default holding areas if none exist."""
    tid = _tid(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = (
        db.query(HoldingArea)
        .filter(HoldingArea.deleted_at.is_(None))
        .count()
    )
    if existing > 0:
        return list_areas(request=request, user=user, db=db)
    now = datetime.now(timezone.utc)
    for i, area_def in enumerate(DEFAULT_AREAS):
        area = HoldingArea(
            id=str(uuid4()),
            company_id=tid,
            name=area_def["name"],
            color=area_def["color"],
            sort_order=i,
            created_at=now,
        )
        db.add(area)
    db.commit()
    return list_areas(request=request, user=user, db=db)
