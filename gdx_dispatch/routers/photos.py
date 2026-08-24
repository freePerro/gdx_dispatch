"""
Photos router — job photo gallery (before/during/after/progress/other) and a
recent photos feed for the dashboard.

Gated behind the "jobs" module. Follows the notes router pattern for tenant
scoping, audit logging, and Pydantic validation.

Upload flow — CORRECTED 2026-08-24. This docstring described a two-step flow
("POST the binary to /api/uploads to obtain a URL, then POST the URL here")
that does not exist. `/api/uploads` was a ui_compat stub with no caller and was
removed; the real path is a DIRECT multipart upload to
`routers/uploads.py::upload_job_photo`.

Note this module's own `create_job_photo` is SHADOWED: both it and
`uploads.upload_job_photo` register `POST /api/jobs/{job_id}/photos`, and
uploads.py is included first, so it is the one that serves. Do not assume the
handler below runs — see docs/design/unimplemented-endpoints-decision-list.md,
"The duplicate-shim trap".
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
from gdx_dispatch.core.job_access import assert_job_access
from gdx_dispatch.core.modules import require_module
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
    # Share this photo with the customer, or take it back (migration 063).
    # False is the default state of every photo; this is the office saying
    # otherwise, per photo.
    customer_visible: bool | None = Field(default=None)


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
        # Whether the CUSTOMER can see this photo (migration 063). Default
        # False — the office shares deliberately, per photo.
        "customer_visible": bool(getattr(p, "customer_visible", False)),
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


# Reading a job's photos is an OFFICE-TIER read, not a dispatch-manager one.
#
# `assert_job_access` admits only DISPATCH_MANAGER_ROLES (owner, admin,
# dispatcher, manager, super_admin) or the technician the job is assigned to.
# But `nav.office` — the key that puts the Photos page and the office nav on
# screen — is granted to accounting, sales and viewer, none of which are
# dispatch managers. Those roles got a 404 here, and PhotosView renders a 404
# as "No photos yet": the app showed office staff an empty gallery and called
# it empty rather than forbidden. Accounting is also the role that bills, and
# the invoice photo picker reads this exact endpoint — so the person putting
# photos on an invoice could never see one.
#
# The rule: anyone who can read every job (dispatcher/sales/viewer) or every
# invoice (accounting — photos print on the invoices they send) may read a
# job's photos. Technicians stay narrowed to their own jobs, which is what
# keeps customer-premises photos off other techs' phones. Writes and deletes
# are untouched.
_PHOTO_READ_KEYS = ("jobs.read_all", "invoices.read_all")


def _assert_photo_read_access(
    db: Session, request: Request, tenant_id: str, user: Any, job_id: str
) -> None:
    if _has_office_photo_read(db, request, user):
        return
    assert_job_access(db, tenant_id, user, job_id)


def _assert_photo_edit_access(
    db: Session, request: Request, tenant_id: str, user: Any, job_id: str
) -> None:
    """Who may edit a photo's slot, caption, or customer-visibility.

    Same tier as the read (2026-08-13). Widening the read without the write
    shipped a control that could not work: accounting/sales/viewer could open
    the job page, see the photos for the first time, click "Internal only" —
    and the PATCH 404'd, so the checkbox flipped back. Worse, accounting could
    already share the same photo through the other door (attaching it to an
    invoice sets customer_visible with only the invoice permission), so the
    job-page gate was withholding a decision the same user could make one
    screen away.

    Technicians remain narrowed to their own jobs, which is what keeps another
    customer's premises off a tech's phone. Deleting a photo is NOT this —
    delete keeps the dispatch-manager gate.
    """
    _assert_photo_read_access(db, request, tenant_id, user, job_id)


def _has_office_photo_read(db: Session, request: Request, user: Any) -> bool:
    """True for dispatch managers and for the office roles above.

    Resolution goes through the same loader `require_permission` uses (role
    snapshot → builtin), so a tenant that edits its roles gets the answer its
    own configuration implies, not a hardcoded role list.
    """
    if is_dispatch_manager(user):
        return True
    try:
        from gdx_dispatch.core.modules import _load_user_permissions
        from gdx_dispatch.core.permissions import WILDCARD

        cached = getattr(request.state, "user_permissions", None)
        if cached is None:
            cached = _load_user_permissions(db, request, user)
            request.state.user_permissions = cached
        return WILDCARD in cached or any(k in cached for k in _PHOTO_READ_KEYS)
    except Exception:
        # A permission lookup that breaks must not silently widen access.
        log.exception("photo_read_permission_check_failed")
        return False


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
    tenant_id = _tenant_id(request)
    _assert_photo_read_access(db, request, tenant_id, user, str(job_id))
    stmt = (
        select(JobPhoto)
        .where(
            JobPhoto.job_id == job_id,
            # Deliberately NOT filtered on company_id, though the sibling
            # _get_photo_scoped is. Two writers populate that column from two
            # different resolutions — uploads.py prefers the JWT's tenant claim
            # (_tenant_id_from), documents.py reads request.state.tenant — so a
            # filter here fails CLOSED the moment they disagree, and failing
            # closed on this query means an empty Photos tab: precisely the bug
            # this change exists to fix. One tenant per database, and isolation
            # is the connection; job ownership is enforced above.
            JobPhoto.deleted_at.is_(None),
        )
        .order_by(JobPhoto.uploaded_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


# DEAD ROUTE — kept only so its history is legible; DO NOT reach for it.
#
# It has never been reachable: uploads.py declares POST on this exact path with
# a multipart signature and is included first (app.py:1551 vs :1602), so
# FastAPI matches that one and this never runs. The Photos page dutifully
# POSTed JSON here and got 422 "field required: file" every single time — which
# is why job_photos has 0 rows despite a working upload and a working UI.
#
# Adding a THIRD handler on the same path would not have helped. The upload
# route now creates the JobPhoto record itself (uploads.py::_link_job_photo),
# so there is one way to add a photo: POST the file to /api/jobs/{id}/photos.
# The GET/PATCH/DELETE below are NOT shadowed (uploads.py only claims POST) and
# remain the live read/edit surface.
@router.post("/api/jobs/{job_id}/photos", response_model=None, status_code=201, include_in_schema=False)
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
    _assert_photo_edit_access(db, request, tenant_id, user, str(job_id))
    photo = _get_photo_scoped(db, photo_id, job_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"] is not None:
        photo.kind = data["kind"]
    if "caption" in data and data["caption"] is not None:
        photo.caption = data["caption"]
    if "customer_visible" in data and data["customer_visible"] is not None:
        photo.customer_visible = bool(data["customer_visible"])
    db.commit()
    db.refresh(photo)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="photo_updated",
        entity_id=str(photo.id),
        # Who shared a customer's photo, and when, is the part of this record
        # worth being able to answer later — so log the VALUE, not just the
        # field name.
        details={
            "fields": list(data.keys()),
            **(
                {"customer_visible": bool(data["customer_visible"])}
                if data.get("customer_visible") is not None
                else {}
            ),
        },
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
    # Tenant-wide photo feed across all jobs — office tier only; a technician
    # would otherwise see customer-premises photos from jobs that aren't theirs.
    # Same rule as the per-job read above (see _has_office_photo_read): the
    # office roles that hold nav.office are the ones this page is FOR.
    if not _has_office_photo_read(db, request, user):
        raise HTTPException(status_code=403, detail="office or dispatch role required")
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
