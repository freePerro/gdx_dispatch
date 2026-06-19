"""Tenant CRUD for customer-alert tag taxonomy.

Sprint tech_mobile S1-A8.

The default taxonomy is seeded on tenant provisioning (see
gdx_dispatch/core/customer_alert_tags.py). Tenants can then manage their own
taxonomy through this router — add new tags, rename, recolor, edit
descriptions, or delete tags they don't use.

Endpoints (all gated on settings.write):
- GET    /api/admin/customer-tags
- POST   /api/admin/customer-tags
- PUT    /api/admin/customer-tags/{tag_id}
- DELETE /api/admin/customer-tags/{tag_id}

Delete behavior: removes the Tag row AND any TagAssignment rows pointing
to it (so a deleted alert no longer shows up on customer cards). The
delete is audit-logged with a count of cascaded assignments. There is no
soft-delete on Tag; the table is small and tenants asked for "delete
means delete" semantics during the 2026-05-02 design conversation.

Renames are non-destructive — TagAssignment references the Tag by id, so
renaming a Tag.name automatically updates every customer card on the
next /api/mobile/today read.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.models.tenant_models import Tag, TagAssignment
from gdx_dispatch.routers.auth import get_current_user


log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/customer-tags",
    tags=["admin", "customer-tags"],
)


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,79}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _tenant_id_from_request(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if tid is None:
        tenant = getattr(request.state, "tenant", None) or {}
        tid = tenant.get("id") if isinstance(tenant, dict) else None
    if tid is None:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return str(tid)


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("user_id") or user.get("id") or user.get("sub") or "unknown")
    return str(getattr(user, "user_id", None) or "unknown")


def _validate_name(name: str) -> str:
    # Strict: reject uppercase / spaces / non-alphanum at validation time
    # rather than silently normalizing. Tenants need a predictable name
    # contract so the seeded taxonomy + admin-typed names follow the same
    # shape rules.
    n = (name or "").strip()
    if not _NAME_RE.match(n):
        raise HTTPException(
            status_code=400,
            detail="tag name: lowercase letters/digits/underscores only, 1–80 chars, must start with letter or digit",
        )
    return n


def _validate_color(color: str) -> str:
    c = (color or "").strip()
    if not _HEX_COLOR_RE.match(c):
        raise HTTPException(
            status_code=400, detail="tag color must be a hex string like #ff0000"
        )
    return c


def _serialize(tag: Tag) -> dict[str, Any]:
    return {
        "id": str(tag.id),
        "name": tag.name,
        "color": tag.color,
        "description": tag.description or "",
    }


# ── pydantic ──────────────────────────────────────────────────────────


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    # Color length is enforced by _validate_color (regex on hex shape) so
    # all malformed-color rejections funnel to a single 400 with a
    # consistent error message rather than splitting between Pydantic 422
    # and our 400.
    color: str = Field(default="#6366f1", min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=500)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    color: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=500)


# ── endpoints ─────────────────────────────────────────────────────────


@router.get("", response_model=None)
def list_customer_tags(
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tenant_id_from_request(request)
    tags = (
        db.query(Tag)
        .filter(Tag.company_id == tid, Tag.deleted_at.is_(None))
        .order_by(Tag.name.asc())
        .all()
    )
    return {"tags": [_serialize(t) for t in tags]}


@router.post("", response_model=None)
def create_customer_tag(
    payload: TagCreate,
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tenant_id_from_request(request)
    name = _validate_name(payload.name)
    color = _validate_color(payload.color)

    tag = Tag(
        id=uuid4(),
        company_id=tid,
        name=name,
        color=color,
        description=(payload.description or "").strip() or None,
    )
    db.add(tag)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"tag {name!r} already exists"
        ) from exc

    try:
        log_audit_event_sync(
            db,
            tenant_id=tid,
            user_id=_user_id(user),
            action="customer_tag.created",
            entity_type="customer_tag",
            entity_id=str(tag.id),
            details={"name": name, "color": color},
            request=request,
        )
    except Exception:
        log.exception("customer_tag_audit_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="audit failure — change rolled back")

    db.commit()
    return _serialize(tag)


@router.put("/{tag_id}", response_model=None)
def update_customer_tag(
    tag_id: UUID,
    payload: TagUpdate,
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tenant_id_from_request(request)
    tag = (
        db.query(Tag)
        .filter(Tag.id == tag_id, Tag.company_id == tid, Tag.deleted_at.is_(None))
        .first()
    )
    if tag is None:
        raise HTTPException(status_code=404, detail="tag not found")

    before = _serialize(tag)
    if payload.name is not None:
        tag.name = _validate_name(payload.name)
    if payload.color is not None:
        tag.color = _validate_color(payload.color)
    if payload.description is not None:
        tag.description = payload.description.strip() or None

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"tag {tag.name!r} already exists"
        ) from exc

    after = _serialize(tag)
    try:
        log_audit_event_sync(
            db,
            tenant_id=tid,
            user_id=_user_id(user),
            action="customer_tag.updated",
            entity_type="customer_tag",
            entity_id=str(tag.id),
            details={"before": before, "after": after},
            request=request,
        )
    except Exception:
        log.exception("customer_tag_audit_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="audit failure — change rolled back")

    db.commit()
    return after


@router.delete("/{tag_id}", response_model=None)
def delete_customer_tag(
    tag_id: UUID,
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tenant_id_from_request(request)
    tag = (
        db.query(Tag)
        .filter(Tag.id == tag_id, Tag.company_id == tid, Tag.deleted_at.is_(None))
        .first()
    )
    if tag is None:
        raise HTTPException(status_code=404, detail="tag not found")
    snapshot = _serialize(tag)

    cascade = (
        db.query(TagAssignment)
        .filter(
            TagAssignment.company_id == tid,
            TagAssignment.tag_id == tag_id,
        )
        .delete(synchronize_session=False)
    )
    db.delete(tag)

    try:
        log_audit_event_sync(
            db,
            tenant_id=tid,
            user_id=_user_id(user),
            action="customer_tag.deleted",
            entity_type="customer_tag",
            entity_id=str(tag_id),
            details={"snapshot": snapshot, "assignments_removed": cascade},
            request=request,
        )
    except Exception:
        log.exception("customer_tag_audit_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="audit failure — change rolled back")

    db.commit()
    return {"ok": True, "id": str(tag_id), "assignments_removed": cascade}
