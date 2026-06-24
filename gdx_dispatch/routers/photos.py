"""
Photos router — job photo gallery (before/during/after/progress/other) and a
recent photos feed for the dashboard.

Gated behind the "jobs" module. Follows the notes router pattern for tenant
scoping, audit logging, and Pydantic validation.

Upload flow: callers first POST the binary to the existing /api/uploads router
to obtain a URL, then POST the URL here to attach it to a job.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.job_access import assert_job_access
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["photos"],
    dependencies=[Depends(require_module("jobs"))],
)


PHOTO_KINDS = ("before", "during", "after", "progress", "other")
_KIND_PATTERN = r"^(before|during|after|progress|other)$"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import JobPhoto  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class PhotoIn(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    kind: str = Field(default="during", pattern=_KIND_PATTERN)
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=100)
    size_bytes: int | None = Field(default=None, ge=0, le=50_000_000)
    caption: str | None = Field(default=None, max_length=500)


class PhotoPatchIn(BaseModel):
    kind: str | None = Field(default=None, pattern=_KIND_PATTERN)
    caption: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _user_name(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("name") or user.get("email") or None


def _serialize(p: JobPhoto) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "company_id": p.company_id,
        "job_id": str(p.job_id),
        "kind": p.kind,
        "url": p.url,
        "filename": p.filename,
        "mime_type": p.mime_type,
        "size_bytes": int(p.size_bytes) if p.size_bytes is not None else None,
        "caption": p.caption,
        "uploaded_by": p.uploaded_by,
        "uploaded_at": p.uploaded_at.isoformat() if p.uploaded_at else None,
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type="job_photo",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("photos_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


def _get_photo_scoped(
    db: Session, photo_id: UUID, job_id: UUID, tenant_id: str
) -> JobPhoto:
    row = db.execute(
        select(JobPhoto).where(
            JobPhoto.id == photo_id,
            JobPhoto.job_id == job_id,
            JobPhoto.company_id == tenant_id,
            JobPhoto.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Photo not found")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/photos", response_model=None)
def list_job_photos(
    job_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    assert_job_access(db, _tenant_id(request), user, str(job_id))
    stmt = (
        select(JobPhoto)
        .where(
            JobPhoto.job_id == job_id,
            JobPhoto.deleted_at.is_(None),
        )
        .order_by(JobPhoto.uploaded_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/jobs/{job_id}/photos", response_model=None, status_code=201)
def create_job_photo(
    job_id: UUID,
    payload: PhotoIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    assert_job_access(db, tenant_id, user, str(job_id))
    photo = JobPhoto(
        company_id=tenant_id,
        job_id=job_id,
        kind=payload.kind,
        url=payload.url,
        filename=payload.filename,
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        caption=payload.caption,
        uploaded_by=_user_name(user) or _user_id(user),
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="photo_created",
        entity_id=str(photo.id),
        details={"job_id": str(job_id), "kind": photo.kind, "filename": photo.filename},
        request=request,
    )
    return _serialize(photo)


@router.patch("/api/jobs/{job_id}/photos/{photo_id}", response_model=None)
def update_job_photo(
    job_id: UUID,
    photo_id: UUID,
    payload: PhotoPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    assert_job_access(db, tenant_id, user, str(job_id))
    photo = _get_photo_scoped(db, photo_id, job_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"] is not None:
        photo.kind = data["kind"]
    if "caption" in data and data["caption"] is not None:
        photo.caption = data["caption"]
    db.commit()
    db.refresh(photo)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="photo_updated",
        entity_id=str(photo.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize(photo)


@router.delete(
    "/api/jobs/{job_id}/photos/{photo_id}", response_model=None, status_code=204
)
def delete_job_photo(
    job_id: UUID,
    photo_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    assert_job_access(db, tenant_id, user, str(job_id))
    photo = _get_photo_scoped(db, photo_id, job_id, tenant_id)
    photo.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="photo_deleted",
        entity_id=str(photo_id),
        details={"job_id": str(job_id)},
        request=request,
    )
    return None


@router.get("/api/photos/recent", response_model=None)
def recent_photos(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict[str, Any]]:
    # Tenant-wide photo feed across all jobs — dispatch/admin only; a technician
    # would otherwise see customer-premises photos from jobs that aren't theirs.
    if not is_dispatch_manager(user):
        raise HTTPException(status_code=403, detail="dispatcher or admin role required")
    stmt = (
        select(JobPhoto)
        .where(
            JobPhoto.deleted_at.is_(None),
        )
        .order_by(JobPhoto.uploaded_at.desc())
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]
