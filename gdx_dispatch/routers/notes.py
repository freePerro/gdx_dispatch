"""
Notes router — job notes (attached to a job, author-gated) and sticky notes
(company-wide canvas with position/color).

Gated behind the "jobs" module. Follows the proposals router pattern for
tenant scoping, audit logging, and Pydantic validation.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["notes"],
    dependencies=[Depends(require_module("jobs"))],
)


VISIBILITY_VALUES = ("internal", "external")
ADMIN_ROLES = ("admin", "owner")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import JobNote, StickyNote  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class JobNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    visibility: str = Field(default="internal", pattern=r"^(internal|external)$")


class JobNotePatchIn(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=10000)
    visibility: str | None = Field(default=None, pattern=r"^(internal|external)$")


class StickyNoteIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    color: str = Field(default="#fef3c7", pattern=r"^#[0-9a-fA-F]{6}$")
    pos_x: int = Field(default=0, ge=-100000, le=100000)
    pos_y: int = Field(default=0, ge=-100000, le=100000)
    width: int = Field(default=240, ge=50, le=2000)
    height: int = Field(default=180, ge=50, le=2000)


class StickyNotePatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=5000)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    pos_x: int | None = Field(default=None, ge=-100000, le=100000)
    pos_y: int | None = Field(default=None, ge=-100000, le=100000)
    width: int | None = Field(default=None, ge=50, le=2000)
    height: int | None = Field(default=None, ge=50, le=2000)


class StickyNoteMoveIn(BaseModel):
    pos_x: int = Field(ge=-100000, le=100000)
    pos_y: int = Field(ge=-100000, le=100000)


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


def _user_role(user: Any) -> str:
    if not isinstance(user, dict):
        return ""
    return str(user.get("role") or "")


def _serialize_job_note(n: JobNote) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "company_id": n.company_id,
        "job_id": str(n.job_id),
        "author_id": n.author_id,
        "author_name": n.author_name,
        "body": n.body,
        "visibility": n.visibility,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


def _serialize_sticky(s: StickyNote) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "company_id": s.company_id,
        "title": s.title,
        "body": s.body,
        "color": s.color,
        "pos_x": int(s.pos_x or 0),
        "pos_y": int(s.pos_y or 0),
        "width": int(s.width or 0),
        "height": int(s.height or 0),
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_type: str,
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
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("notes_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


def _get_job_note_scoped(
    db: Session, note_id: UUID, job_id: UUID, tenant_id: str
) -> JobNote:
    row = db.execute(
        select(JobNote).where(
            JobNote.id == str(note_id),
            JobNote.job_id == str(job_id),
            JobNote.company_id == tenant_id,
            JobNote.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Job note not found")
    return row


def _get_sticky_scoped(db: Session, note_id: UUID, tenant_id: str) -> StickyNote:
    row = db.execute(
        select(StickyNote).where(
            StickyNote.id == note_id,
            StickyNote.company_id == tenant_id,
            StickyNote.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Sticky note not found")
    return row


def _author_gate(note: JobNote, user: Any) -> None:
    if note.author_id == _user_id(user):
        return
    if _user_role(user) in ADMIN_ROLES:
        return
    raise HTTPException(
        status_code=403, detail="Only the author or an admin can modify this note"
    )


# ---------------------------------------------------------------------------
# Job notes endpoints
# ---------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/notes", response_model=None)
def list_job_notes(
    job_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(JobNote)
        .where(
            JobNote.job_id == str(job_id),
            JobNote.deleted_at.is_(None),
        )
        .order_by(JobNote.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_job_note(r) for r in rows]


@router.post("/api/jobs/{job_id}/notes", response_model=None, status_code=201)
def create_job_note(
    job_id: UUID,
    payload: JobNoteIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    note = JobNote(
        company_id=tenant_id,
        job_id=str(job_id),
        author_id=_user_id(user),
        author_name=_user_name(user),
        body=payload.body,
        visibility=payload.visibility,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="job_note_created",
        entity_type="job_note",
        entity_id=str(note.id),
        details={"job_id": str(job_id), "visibility": note.visibility},
        request=request,
    )
    return _serialize_job_note(note)


@router.patch("/api/jobs/{job_id}/notes/{note_id}", response_model=None)
def update_job_note(
    job_id: UUID,
    note_id: UUID,
    payload: JobNotePatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    note = _get_job_note_scoped(db, note_id, job_id, tenant_id)
    _author_gate(note, user)
    data = payload.model_dump(exclude_unset=True)
    if "body" in data and data["body"] is not None:
        note.body = data["body"]
    if "visibility" in data and data["visibility"] is not None:
        note.visibility = data["visibility"]
    db.commit()
    db.refresh(note)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="job_note_updated",
        entity_type="job_note",
        entity_id=str(note.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize_job_note(note)


@router.delete(
    "/api/jobs/{job_id}/notes/{note_id}", response_model=None, status_code=204
)
def delete_job_note(
    job_id: UUID,
    note_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    note = _get_job_note_scoped(db, note_id, job_id, tenant_id)
    _author_gate(note, user)
    note.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="job_note_deleted",
        entity_type="job_note",
        entity_id=str(note_id),
        details={"job_id": str(job_id)},
        request=request,
    )
    return None


# ---------------------------------------------------------------------------
# Sticky notes endpoints
# ---------------------------------------------------------------------------


@router.get("/api/sticky-notes", response_model=None)
def list_sticky_notes(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(StickyNote)
        .where(
            StickyNote.deleted_at.is_(None),
        )
        .order_by(StickyNote.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_sticky(r) for r in rows]


@router.post("/api/sticky-notes", response_model=None, status_code=201)
def create_sticky_note(
    payload: StickyNoteIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    s = StickyNote(
        company_id=tenant_id,
        title=payload.title,
        body=payload.body,
        color=payload.color,
        pos_x=payload.pos_x,
        pos_y=payload.pos_y,
        width=payload.width,
        height=payload.height,
        created_by=_user_id(user),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="sticky_note_created",
        entity_type="sticky_note",
        entity_id=str(s.id),
        details={"title": s.title},
        request=request,
    )
    return _serialize_sticky(s)


@router.patch("/api/sticky-notes/{note_id}", response_model=None)
def update_sticky_note(
    note_id: UUID,
    payload: StickyNotePatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    s = _get_sticky_scoped(db, note_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ("title", "body", "color", "pos_x", "pos_y", "width", "height"):
        if field in data and data[field] is not None:
            setattr(s, field, data[field])
    db.commit()
    db.refresh(s)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="sticky_note_updated",
        entity_type="sticky_note",
        entity_id=str(s.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize_sticky(s)


@router.delete("/api/sticky-notes/{note_id}", response_model=None, status_code=204)
def delete_sticky_note(
    note_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    s = _get_sticky_scoped(db, note_id, tenant_id)
    s.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="sticky_note_deleted",
        entity_type="sticky_note",
        entity_id=str(note_id),
        request=request,
    )
    return None


@router.post("/api/sticky-notes/{note_id}/move", response_model=None)
def move_sticky_note(
    note_id: UUID,
    payload: StickyNoteMoveIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    s = _get_sticky_scoped(db, note_id, tenant_id)
    s.pos_x = payload.pos_x
    s.pos_y = payload.pos_y
    db.commit()
    db.refresh(s)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="sticky_note_moved",
        entity_type="sticky_note",
        entity_id=str(s.id),
        details={"pos_x": s.pos_x, "pos_y": s.pos_y},
        request=request,
    )
    return _serialize_sticky(s)
