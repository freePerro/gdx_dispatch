"""Warranty Claims — file, track, and resolve warranty claims."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import WarrantyClaim
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/warranty-claims",
    tags=["warranty-claims"],
    dependencies=[Depends(require_module("jobs"))],
)

CLAIM_STATUSES = ("filed", "pending", "approved", "denied", "replaced")


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClaimIn(BaseModel):
    warranty_id: str | None = Field(default=None, max_length=36)
    job_id: str | None = Field(default=None, max_length=36)
    customer_id: str = Field(min_length=1, max_length=36)
    serial_number: str | None = Field(default=None, max_length=120)
    manufacturer: str | None = Field(default=None, max_length=200)
    claim_notes: str | None = Field(default=None, max_length=5000)


class ClaimPatch(BaseModel):
    status: str | None = Field(default=None, max_length=30)
    resolution: str | None = Field(default=None, max_length=5000)
    claim_notes: str | None = Field(default=None, max_length=5000)


def _serialize(c: WarrantyClaim) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "company_id": str(c.company_id),
        "warranty_id": str(c.warranty_id) if c.warranty_id else None,
        "job_id": str(c.job_id) if c.job_id else None,
        "customer_id": str(c.customer_id),
        "serial_number": c.serial_number,
        "manufacturer": c.manufacturer,
        "status": c.status,
        "claim_notes": c.claim_notes,
        "filed_at": str(c.filed_at) if c.filed_at else None,
        "resolved_at": str(c.resolved_at) if c.resolved_at else None,
        "resolution": c.resolution,
        "created_by": str(c.created_by),
        "created_at": str(c.created_at) if c.created_at else None,
        "updated_at": str(c.updated_at) if c.updated_at else None,
    }


@router.post("", status_code=201)
def file_claim(
    request: Request,
    payload: ClaimIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    now = _now()
    try:
        claim = WarrantyClaim(
            id=uuid4(), company_id=tid, warranty_id=payload.warranty_id,
            job_id=payload.job_id, customer_id=payload.customer_id,
            serial_number=payload.serial_number, manufacturer=payload.manufacturer,
            status="filed", claim_notes=payload.claim_notes,
            filed_at=now, created_by=uid, created_at=now, updated_at=now,
        )
        db.add(claim)
        db.commit()
        db.refresh(claim)
    except Exception:
        db.rollback()
        log.exception("warranty_claim_create_failed")
        raise HTTPException(status_code=500, detail="Failed to file warranty claim") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="warranty_claim", entity_id=str(claim.id),
        details={"customer_id": payload.customer_id, "manufacturer": payload.manufacturer,
                 "serial_number": payload.serial_number},
        request=request,
    )
    return _serialize(claim)


@router.get("")
def list_claims(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = Query(None),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(WarrantyClaim).where(
        WarrantyClaim.deleted_at.is_(None),
    )
    if status:
        if status not in CLAIM_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {', '.join(CLAIM_STATUSES)}")
        q = q.where(WarrantyClaim.status == status)
    q = q.order_by(WarrantyClaim.filed_at.desc())
    return [_serialize(c) for c in db.execute(q).scalars().all()]


@router.patch("/{claim_id}")
def update_claim(
    claim_id: str,
    request: Request,
    payload: ClaimPatch,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    claim = db.execute(
        select(WarrantyClaim).where(
            WarrantyClaim.id == claim_id,
            WarrantyClaim.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not claim:
        raise HTTPException(status_code=404, detail="Warranty claim not found")

    now = _now()
    claim.updated_at = now

    if payload.status is not None:
        if payload.status not in CLAIM_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {', '.join(CLAIM_STATUSES)}")
        claim.status = payload.status
        if payload.status in ("approved", "denied", "replaced"):
            claim.resolved_at = now
    if payload.resolution is not None:
        claim.resolution = payload.resolution
    if payload.claim_notes is not None:
        claim.claim_notes = payload.claim_notes

    try:
        db.commit()
        db.refresh(claim)
    except Exception:
        db.rollback()
        log.exception("warranty_claim_update_failed")
        raise HTTPException(status_code=500, detail="Failed to update warranty claim") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="update",
        entity_type="warranty_claim", entity_id=claim_id,
        details={"changes": payload.model_dump(exclude_none=True)},
        request=request,
    )
    return _serialize(claim)


@router.get("/{claim_id}")
def get_claim(
    claim_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    claim = db.execute(
        select(WarrantyClaim).where(
            WarrantyClaim.id == claim_id,
            WarrantyClaim.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not claim:
        raise HTTPException(status_code=404, detail="Warranty claim not found")
    return _serialize(claim)
