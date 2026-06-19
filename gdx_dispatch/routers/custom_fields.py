"""
Custom Fields router — per-tenant user-defined fields for customers/jobs.

Provides:
- Definitions CRUD (admin-gated) for `customer` and `job` entity types
- Value upsert/read endpoints per entity (job/customer)

All queries are tenant-scoped against ``request.state.tenant["id"]``.
Schema is defined inline (matches collections.py / change_orders.py pattern).
The legacy broken router at ``gdx_dispatch/core/custom_fields_router.py`` is
deliberately NOT touched — this module is the canonical one going forward.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["custom_fields"],
    dependencies=[Depends(require_module("jobs")), Depends(require_role("admin", "owner", "superadmin"))],
)


ENTITY_TYPES = ("customer", "job")
FIELD_TYPES = ("text", "number", "date", "select", "boolean")


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class CustomFieldDefinition(TenantBase):
    __tablename__ = "custom_field_definitions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)
    options: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "entity_type", "field_key", name="uq_custom_field_key"
        ),
    )


class CustomFieldValue(TenantBase):
    __tablename__ = "custom_field_values"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    definition_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "definition_id", "entity_id", name="uq_custom_field_value"
        ),
        {"extend_existing": True},
    )


# ---------------------------------------------------------------------------
# Pydantic schemas (all strings bounded — Build Rule: Input Validation)
# ---------------------------------------------------------------------------

class CustomFieldDefinitionIn(BaseModel):
    entity_type: str = Field(pattern=r"^(customer|job)$", max_length=30)
    field_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    field_type: str = Field(pattern=r"^(text|number|date|select|boolean)$", max_length=30)
    options: list[str] | None = Field(default=None, max_length=100)
    required: bool = False
    sort_order: int = Field(default=0, ge=0, le=9999)


class CustomFieldDefinitionPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    field_type: str | None = Field(
        default=None, pattern=r"^(text|number|date|select|boolean)$", max_length=30
    )
    options: list[str] | None = Field(default=None, max_length=100)
    required: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class CustomFieldValueUpsert(BaseModel):
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tid = str(tenant.get("id") or "")
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or "system")
    return "system"


def _serialize_definition(d: CustomFieldDefinition) -> dict[str, Any]:
    parsed_options: list[str] | None = None
    if d.options:
        try:
            raw = json.loads(d.options)
            if isinstance(raw, list):
                parsed_options = [str(x) for x in raw]
        except (ValueError, TypeError):
            log.exception("custom_field_options_parse_failed id=%s", d.id)
            parsed_options = None
    return {
        "id": str(d.id),
        "entity_type": d.entity_type,
        "field_key": d.field_key,
        "label": d.label,
        "field_type": d.field_type,
        "options": parsed_options,
        "required": bool(d.required),
        "sort_order": int(d.sort_order or 0),
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _audit(
    db: Session,
    request: Request,
    user: Any,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=_tenant_id(request),
            user_id=_user_id(user),
            action=action,
            entity_type="custom_field",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("custom_field_audit_failed action=%s entity_id=%s", action, entity_id)


def _coerce_value_to_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    return str(raw)


# ---------------------------------------------------------------------------
# Definition endpoints
# ---------------------------------------------------------------------------

@router.get("/api/custom-fields", response_model=None)
def list_definitions(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    stmt = select(CustomFieldDefinition).where(
        CustomFieldDefinition.company_id == tenant_id,
        CustomFieldDefinition.deleted_at.is_(None),
    )
    if entity_type:
        if entity_type not in ENTITY_TYPES:
            raise HTTPException(status_code=422, detail="Invalid entity_type")
        stmt = stmt.where(CustomFieldDefinition.entity_type == entity_type)
    stmt = stmt.order_by(
        CustomFieldDefinition.entity_type,
        CustomFieldDefinition.sort_order,
        CustomFieldDefinition.created_at,
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_definition(r) for r in rows]


@router.post(
    "/api/custom-fields",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def create_definition(
    payload: CustomFieldDefinitionIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)

    # Uniqueness check (company_id + entity_type + field_key)
    existing = db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.company_id == tenant_id,
            CustomFieldDefinition.entity_type == payload.entity_type,
            CustomFieldDefinition.field_key == payload.field_key,
            CustomFieldDefinition.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"field_key '{payload.field_key}' already exists for {payload.entity_type}",
        )

    if payload.field_type == "select" and not payload.options:
        raise HTTPException(
            status_code=422, detail="select field_type requires non-empty options"
        )

    options_json = json.dumps(payload.options) if payload.options is not None else None

    defn = CustomFieldDefinition(
        id=uuid4(),
        company_id=tenant_id,
        entity_type=payload.entity_type,
        field_key=payload.field_key,
        label=payload.label,
        field_type=payload.field_type,
        options=options_json,
        required=payload.required,
        sort_order=payload.sort_order,
    )
    db.add(defn)
    db.commit()
    db.refresh(defn)

    _audit(
        db,
        request,
        user,
        action="custom_field_created",
        entity_id=str(defn.id),
        details={
            "entity_type": defn.entity_type,
            "field_key": defn.field_key,
            "field_type": defn.field_type,
        },
    )
    return _serialize_definition(defn)


@router.patch(
    "/api/custom-fields/{definition_id}",
    response_model=None,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def update_definition(
    definition_id: UUID,
    payload: CustomFieldDefinitionPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    defn = db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.id == definition_id,
            CustomFieldDefinition.company_id == tenant_id,
            CustomFieldDefinition.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not defn:
        raise HTTPException(status_code=404, detail="Custom field definition not found")

    changed: dict[str, Any] = {}
    if payload.label is not None:
        defn.label = payload.label
        changed["label"] = payload.label
    if payload.field_type is not None:
        defn.field_type = payload.field_type
        changed["field_type"] = payload.field_type
    if payload.options is not None:
        defn.options = json.dumps(payload.options)
        changed["options"] = payload.options
    if payload.required is not None:
        defn.required = payload.required
        changed["required"] = payload.required
    if payload.sort_order is not None:
        defn.sort_order = payload.sort_order
        changed["sort_order"] = payload.sort_order

    db.commit()
    db.refresh(defn)

    _audit(
        db,
        request,
        user,
        action="custom_field_updated",
        entity_id=str(defn.id),
        details=changed,
    )
    return _serialize_definition(defn)


@router.delete(
    "/api/custom-fields/{definition_id}",
    response_model=None,
    status_code=204,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def delete_definition(
    definition_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    defn = db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.id == definition_id,
            CustomFieldDefinition.company_id == tenant_id,
            CustomFieldDefinition.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not defn:
        raise HTTPException(status_code=404, detail="Custom field definition not found")

    defn.deleted_at = utcnow()
    db.commit()

    _audit(
        db,
        request,
        user,
        action="custom_field_deleted",
        entity_id=str(definition_id),
        details={"entity_type": defn.entity_type, "field_key": defn.field_key},
    )
    return None


# ---------------------------------------------------------------------------
# Value endpoints (job + customer)
# ---------------------------------------------------------------------------

def _list_values_for_entity(
    db: Session, tenant_id: str, entity_type: str, entity_id: str
) -> list[dict[str, Any]]:
    # Pull definitions + any values, left-joined in Python for simplicity.
    defs = db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.company_id == tenant_id,
            CustomFieldDefinition.entity_type == entity_type,
            CustomFieldDefinition.deleted_at.is_(None),
        ).order_by(CustomFieldDefinition.sort_order, CustomFieldDefinition.created_at)
    ).scalars().all()

    if not defs:
        return []

    def_ids = [d.id for d in defs]
    values = db.execute(
        select(CustomFieldValue).where(
            CustomFieldValue.company_id == tenant_id,
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == str(entity_id),
            CustomFieldValue.definition_id.in_(def_ids),
        )
    ).scalars().all()
    value_by_def = {v.definition_id: v for v in values}

    out: list[dict[str, Any]] = []
    for d in defs:
        serial = _serialize_definition(d)
        v = value_by_def.get(d.id)
        serial["value"] = v.value if v else None
        out.append(serial)
    return out


def _upsert_values_for_entity(
    db: Session,
    request: Request,
    user: Any,
    entity_type: str,
    entity_id: str,
    payload: CustomFieldValueUpsert,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)

    defs = db.execute(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.company_id == tenant_id,
            CustomFieldDefinition.entity_type == entity_type,
            CustomFieldDefinition.deleted_at.is_(None),
        )
    ).scalars().all()
    def_by_key = {d.field_key: d for d in defs}

    unknown = [k for k in payload.values if k not in def_by_key]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown field_key(s) for {entity_type}: {sorted(unknown)}",
        )

    updated_keys: list[str] = []
    now = utcnow()
    for field_key, raw in payload.values.items():
        defn = def_by_key[field_key]
        existing = db.execute(
            select(CustomFieldValue).where(
                CustomFieldValue.company_id == tenant_id,
                CustomFieldValue.definition_id == defn.id,
                CustomFieldValue.entity_id == str(entity_id),
            )
        ).scalar_one_or_none()
        str_val = _coerce_value_to_str(raw)
        if existing:
            existing.value = str_val
            existing.updated_at = now
        else:
            db.add(
                CustomFieldValue(
                    id=uuid4(),
                    company_id=tenant_id,
                    definition_id=defn.id,
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    value=str_val,
                    updated_at=now,
                )
            )
        updated_keys.append(field_key)

    db.commit()

    _audit(
        db,
        request,
        user,
        action="custom_field_value_set",
        entity_id=str(entity_id),
        details={"entity_type": entity_type, "keys": updated_keys},
    )

    return _list_values_for_entity(db, tenant_id, entity_type, entity_id)


@router.get("/api/jobs/{job_id}/custom-fields", response_model=None)
def get_job_custom_fields(
    job_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    return _list_values_for_entity(db, tenant_id, "job", job_id)


@router.put("/api/jobs/{job_id}/custom-fields", response_model=None)
def put_job_custom_fields(
    job_id: str,
    payload: CustomFieldValueUpsert,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return _upsert_values_for_entity(db, request, user, "job", job_id, payload)


@router.get("/api/customers/{customer_id}/custom-fields", response_model=None)
def get_customer_custom_fields(
    customer_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    return _list_values_for_entity(db, tenant_id, "customer", customer_id)


@router.put("/api/customers/{customer_id}/custom-fields", response_model=None)
def put_customer_custom_fields(
    customer_id: str,
    payload: CustomFieldValueUpsert,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return _upsert_values_for_entity(db, request, user, "customer", customer_id, payload)
