"""
Tags router — per-tenant labelled tags assignable to customers and jobs.

Inline models (TenantBase) following the pattern in gdx_dispatch/routers/collections.py.
CRUD + audit pattern follows gdx_dispatch/routers/change_orders.py.

Module gate: "core" is not a registered module key in gdx_dispatch/core/modules.py, so
this router falls back to the "jobs" module (default-on for every tier),
which is appropriate since tags target jobs + customers.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.job_access import assert_can_attach_to_job
from gdx_dispatch.core.modules import MODULES, require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)


# "core" isn't a registered module; fall back to "jobs" per CLAUDE.md rule.
_MODULE_GATE = "core" if "core" in MODULES else "jobs"

router = APIRouter(
    tags=["tags_router"],
    dependencies=[Depends(require_module(_MODULE_GATE))],
)


ENTITY_TYPES = ("customer", "job")


from gdx_dispatch.models.tenant_models import Tag, TagAssignment  # noqa: E402

# --------------------------------------------------------------------------- #
# Pydantic schemas
# --------------------------------------------------------------------------- #


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = Field(default=None, max_length=500)


class TagPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = Field(default=None, max_length=500)


class AssignTagIn(BaseModel):
    tag_id: str = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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


def _serialize_tag(t: Tag) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "company_id": t.company_id,
        "name": t.name,
        "color": t.color,
        "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _serialize_assignment(a: TagAssignment) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "tag_id": str(a.tag_id),
        "entity_type": a.entity_type,
        "entity_id": a.entity_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _get_scoped_tag(db: Session, tag_id: UUID, tenant_id: str) -> Tag:
    row = db.execute(
        select(Tag).where(
            Tag.id == tag_id,
            Tag.company_id == tenant_id,
            Tag.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Tag not found")
    return row


def _parse_tag_id(raw: str) -> UUID:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid tag_id") from None


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
            entity_type="tag",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("tag_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


# --------------------------------------------------------------------------- #
# Tag CRUD
# --------------------------------------------------------------------------- #


@router.get("/api/tags", response_model=None)
def list_tags(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(Tag)
        .where(Tag.deleted_at.is_(None))
        .order_by(Tag.name.asc())
    )
    return [_serialize_tag(t) for t in db.execute(stmt).scalars().all()]


@router.post("/api/tags", response_model=None, status_code=201)
def create_tag(
    payload: TagIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Tag name cannot be blank")

    # Pre-check to convert uniqueness violations into 409 before hitting the DB
    # (also resurrects any existing soft-deleted tag with the same name).
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.execute(
        select(Tag).where(Tag.name == name)
    ).scalar_one_or_none()
    if existing:
        if existing.deleted_at is None:
            raise HTTPException(status_code=409, detail="Tag name already exists")
        existing.deleted_at = None
        existing.color = payload.color
        existing.description = payload.description
        existing.updated_at = utcnow()
        db.commit()
        db.refresh(existing)
        _audit(
            db,
            tenant_id=tenant_id,
            user=user,
            action="tag_created",
            entity_id=str(existing.id),
            details={"name": name, "resurrected": True},
            request=request,
        )
        return _serialize_tag(existing)

    tag = Tag(
        company_id=tenant_id,
        name=name,
        color=payload.color,
        description=payload.description,
    )
    db.add(tag)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Tag name already exists") from None
    db.refresh(tag)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tag_created",
        entity_id=str(tag.id),
        details={"name": name},
        request=request,
    )
    return _serialize_tag(tag)


@router.patch("/api/tags/{tag_id}", response_model=None)
def update_tag(
    tag_id: UUID,
    payload: TagPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    tag = _get_scoped_tag(db, tag_id, tenant_id)

    changed: dict[str, Any] = {}
    before = {"name": tag.name, "color": tag.color, "description": tag.description}
    if payload.name is not None:
        new_name = payload.name.strip()
        if new_name and new_name != tag.name:
            # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
            conflict = db.execute(
                select(Tag).where(
                    Tag.name == new_name,
                    Tag.id != tag.id,
                    Tag.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if conflict:
                raise HTTPException(status_code=409, detail="Tag name already exists")
            tag.name = new_name
            changed["name"] = new_name
    if payload.color is not None:
        tag.color = payload.color
        changed["color"] = payload.color
    if payload.description is not None:
        tag.description = payload.description
        changed["description"] = payload.description

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Tag name already exists") from None
    db.refresh(tag)
    # The before-image: a rename or recolour otherwise loses the old value.
    # Flat `<field>_old` scalars, which the Activity list can print.
    for f in ("name", "color", "description"):
        if f in changed and before[f] != changed[f]:
            changed[f"{f}_old"] = before[f]
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tag_updated",
        entity_id=str(tag.id),
        details=changed,
        request=request,
    )
    return _serialize_tag(tag)


@router.delete("/api/tags/{tag_id}", response_model=None, status_code=204)
def delete_tag(
    tag_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    tag = _get_scoped_tag(db, tag_id, tenant_id)
    tag.deleted_at = utcnow()

    # Drop every assignment that references this tag (scoped to tenant).
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    assignments = db.execute(
        select(TagAssignment).where(
            TagAssignment.tag_id == tag.id,
        )
    ).scalars().all()
    for a in assignments:
        db.delete(a)

    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tag_deleted",
        entity_id=str(tag.id),
        details={"assignments_removed": len(assignments)},
        request=request,
    )
    return None


# --------------------------------------------------------------------------- #
# Assignment helpers
# --------------------------------------------------------------------------- #


def _list_tags_for_entity(
    db: Session, tenant_id: str, entity_type: str, entity_id: str
) -> list[dict[str, Any]]:
    stmt = (
        select(Tag, TagAssignment)
        .join(TagAssignment, TagAssignment.tag_id == Tag.id)
        .where(
            Tag.company_id == tenant_id,
            TagAssignment.company_id == tenant_id,
            TagAssignment.entity_type == entity_type,
            _entity_id_matches(entity_id),
            Tag.deleted_at.is_(None),
        )
        .order_by(Tag.name.asc())
    )
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for tag, assignment in db.execute(stmt).all():
        # Two spellings of one id can each carry the tag; show it once.
        if tag.id in seen:
            continue
        seen.add(tag.id)
        row = _serialize_tag(tag)
        row["assignment_id"] = str(assignment.id)
        out.append(row)
    return out


def canonical_entity_id(value: Any) -> str | None:
    """Dashed lowercase UUID text, the spelling `entity_id` is written in.

    SQLite hands a raw-SQL `customers.id` back as 32 dashless hex; Postgres as
    a UUID. Both canonicalize to the one string `_assign_tag` stores.
    """
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):  # noqa: silent-failure — None for a non-UUID is the contract; callers skip or 422
        log.debug("canonical_entity_id: value is not a UUID", exc_info=True)
        return None


def _dashless(column: Any) -> Any:
    return func.lower(func.replace(column, "-", ""))


def _entity_id_matches(entity_id: str) -> Any:
    """Match `entity_id` in any spelling of the same UUID.

    Before GDXA-1046 the customer routes stored the path id as typed, so a row
    can hold an uppercase or dashless spelling, and one customer can hold the
    same tag under two spellings (the unique constraint is on the raw string).
    The readers and writers in this module find every spelling, so the list
    filter, the row's tags and the segment rule agree. A non-UUID id (none
    today) still matches exactly.
    """
    key = canonical_entity_id(entity_id)
    if key is None:
        return TagAssignment.entity_id == entity_id
    return _dashless(TagAssignment.entity_id) == key.replace("-", "")


def customer_tags_by_id(
    db: Session, customer_ids: list[Any] | None = None
) -> dict[str, list[dict[str, str]]]:
    """``{customer id: [{id, name, color}, ...]}``, live tags only, by name.

    One query for any number of customers: the customer list batches its page
    through it and the segment rollup passes ``None`` for every customer
    (GDXA-1046). Keys are canonical customer ids; a soft-deleted tag is gone
    from every reader, as it is from ``_list_tags_for_entity``.
    """
    stmt = (
        select(TagAssignment.entity_id, Tag)
        .join(Tag, Tag.id == TagAssignment.tag_id)
        .where(TagAssignment.entity_type == "customer", Tag.deleted_at.is_(None))
        .order_by(Tag.name.asc())
    )
    if customer_ids is not None:
        keys = [k for k in (canonical_entity_id(c) for c in customer_ids) if k]
        if not keys:
            return {}
        stmt = stmt.where(
            _dashless(TagAssignment.entity_id).in_([k.replace("-", "") for k in keys])
        )
    out: dict[str, list[dict[str, str]]] = {}
    for entity_id, tag in db.execute(stmt).all():
        key = canonical_entity_id(entity_id)
        if key is None:
            continue
        tags = out.setdefault(key, [])
        # Two spellings of one id can each carry the tag; show it once.
        if all(t["id"] != str(tag.id) for t in tags):
            tags.append({"id": str(tag.id), "name": tag.name, "color": tag.color})
    return out


def live_tag_or_none(db: Session, raw_id: Any) -> Tag | None:
    """The live tag with this id, or None for a malformed, unknown or deleted id."""
    try:
        tag_uuid = UUID(str(raw_id))
    except (ValueError, TypeError, AttributeError):  # noqa: silent-failure — None for a malformed id is the contract; callers 422
        log.debug("live_tag_or_none: tag id is not a UUID", exc_info=True)
        return None
    return db.execute(
        select(Tag).where(Tag.id == tag_uuid, Tag.deleted_at.is_(None))
    ).scalar_one_or_none()


def _find_assignments(
    db: Session, tenant_id: str, tag_id: UUID, entity_type: str, entity_id: str
) -> list[TagAssignment]:
    """Every row tying this tag to this entity, one per stored spelling."""
    return list(db.execute(
        select(TagAssignment).where(
            TagAssignment.company_id == tenant_id,
            TagAssignment.tag_id == tag_id,
            TagAssignment.entity_type == entity_type,
            _entity_id_matches(entity_id),
        )
    ).scalars().all())


def _assign_tag(
    db: Session,
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
    tag_id: UUID,
    user: Any,
    request: Request,
) -> dict[str, Any]:
    # Tag must exist and belong to this tenant.
    tag = _get_scoped_tag(db, tag_id, tenant_id)
    if entity_type == "customer":
        # One spelling on write, so the customer list, its tag filter and the
        # segment rule all key the same row (GDXA-1046).
        entity_id = canonical_entity_id(entity_id) or entity_id

    existing = _find_assignments(db, tenant_id, tag.id, entity_type, entity_id)
    if existing:
        return _serialize_assignment(existing[0])

    assignment = TagAssignment(
        company_id=tenant_id,
        tag_id=tag.id,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_assignments(db, tenant_id, tag.id, entity_type, entity_id)
        if existing:
            return _serialize_assignment(existing[0])
        raise HTTPException(status_code=409, detail="Assignment conflict") from None
    db.refresh(assignment)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tag_assigned",
        entity_id=str(tag.id),
        details={"entity_type": entity_type, "entity_id": entity_id},
        request=request,
    )
    return _serialize_assignment(assignment)


def _unassign_tag(
    db: Session,
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
    tag_id: UUID,
    user: Any,
    request: Request,
) -> None:
    assignments = _find_assignments(db, tenant_id, tag_id, entity_type, entity_id)
    if not assignments:
        raise HTTPException(status_code=404, detail="Tag assignment not found")
    # Untagging removes the tag in every spelling, or a second row keeps it on.
    for assignment in assignments:
        db.delete(assignment)
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tag_unassigned",
        entity_id=str(tag_id),
        details={"entity_type": entity_type, "entity_id": entity_id},
        request=request,
    )


# --------------------------------------------------------------------------- #
# Job assignments
# --------------------------------------------------------------------------- #


@router.get("/api/jobs/{job_id}/tags", response_model=None)
def list_job_tags(
    job_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    return _list_tags_for_entity(db, tenant_id, "job", job_id)


@router.post("/api/jobs/{job_id}/tags", response_model=None, status_code=201)
def assign_tag_to_job(
    job_id: str,
    payload: AssignTagIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    # Object-level authz on the job this write attaches to (#518 sweep).
    # `require_module` asks whether the feature is on for the tenant, never
    # who the caller is, so this route accepted any job id from any
    # authenticated technician. Same opaque 404 as the mobile read gates.
    assert_can_attach_to_job(db, tenant_id, user, job_id)
    tag_uuid = _parse_tag_id(payload.tag_id)
    return _assign_tag(
        db,
        tenant_id=tenant_id,
        entity_type="job",
        entity_id=job_id,
        tag_id=tag_uuid,
        user=user,
        request=request,
    )


@router.delete("/api/jobs/{job_id}/tags/{tag_id}", response_model=None, status_code=204)
def unassign_tag_from_job(
    job_id: str,
    tag_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    # Object-level authz on the job this write attaches to (#518 sweep).
    # `require_module` asks whether the feature is on for the tenant, never
    # who the caller is, so this route accepted any job id from any
    # authenticated technician. Same opaque 404 as the mobile read gates.
    assert_can_attach_to_job(db, tenant_id, user, job_id)
    _unassign_tag(
        db,
        tenant_id=tenant_id,
        entity_type="job",
        entity_id=job_id,
        tag_id=tag_id,
        user=user,
        request=request,
    )
    return None


# --------------------------------------------------------------------------- #
# Customer assignments
# --------------------------------------------------------------------------- #


@router.get("/api/customers/{customer_id}/tags", response_model=None)
def list_customer_tags(
    customer_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    return _list_tags_for_entity(db, tenant_id, "customer", customer_id)


@router.post("/api/customers/{customer_id}/tags", response_model=None, status_code=201)
def assign_tag_to_customer(
    customer_id: str,
    payload: AssignTagIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    tag_uuid = _parse_tag_id(payload.tag_id)
    return _assign_tag(
        db,
        tenant_id=tenant_id,
        entity_type="customer",
        entity_id=customer_id,
        tag_id=tag_uuid,
        user=user,
        request=request,
    )


@router.delete(
    "/api/customers/{customer_id}/tags/{tag_id}",
    response_model=None,
    status_code=204,
)
def unassign_tag_from_customer(
    customer_id: str,
    tag_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    _unassign_tag(
        db,
        tenant_id=tenant_id,
        entity_type="customer",
        entity_id=customer_id,
        tag_id=tag_id,
        user=user,
        request=request,
    )
    return None
