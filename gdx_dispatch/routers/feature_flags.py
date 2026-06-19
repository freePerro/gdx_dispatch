"""
Feature Flags router — per-tenant feature toggle management.

Provides tenant-scoped, persistent feature flags (distinct from the
superadmin rollout system in ``gdx_dispatch/core/feature_flags_router.py`` which
manages platform-wide flags in the control plane).

Routes are mounted under ``/api/tenant/feature-flags`` to avoid
collision with the superadmin ``/api/feature-flags`` endpoint.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/tenant/feature-flags",
    tags=["feature_flags"],
    dependencies=[Depends(require_role("admin", "owner", "superadmin"))],
)


# ---------------------------------------------------------------------------
# Model (inline, TenantBase pattern — matches collections.py)
# ---------------------------------------------------------------------------


class FeatureFlag(TenantBase):
    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_feature_flags_company_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flag_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rollout_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class FeatureFlagIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    enabled: bool = Field(default=False)
    description: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_identity(user: dict | None) -> tuple[str, str]:
    user = user or {}
    user_id = str(user.get("sub") or user.get("user_id") or "system")
    display = str(user.get("email") or user.get("sub") or user.get("user_id") or "system")
    return user_id, display


def _serialize(f: FeatureFlag) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "name": f.name,
        "enabled": bool(f.enabled),
        "description": f.description,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
        "updated_by": f.updated_by,
    }


def _audit(
    db: Session,
    request: Request | None,
    user: dict | None,
    action: str,
    entity_id: str,
    details: dict[str, Any],
) -> None:
    try:
        tenant_id = ""
        if request is not None:
            tenant_id = str(
                (getattr(getattr(request, "state", None), "tenant", {}) or {}).get("id") or ""
            )
        user_id, _ = _user_identity(user)
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            entity_type="feature_flag",
            entity_id=entity_id,
            details=details,
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("feature_flag_audit_failed action=%s entity_id=%s", action, entity_id)


def _get_flag(db: Session, company_id: str, name: str) -> FeatureFlag | None:
    stmt = select(FeatureFlag).where(
        FeatureFlag.company_id == company_id,
        FeatureFlag.name == name,
    )
    return db.execute(stmt).scalars().first()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=None)
def list_feature_flags(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List all feature flags for the current tenant."""
    company_id = _tenant_id(request)
    stmt = (
        select(FeatureFlag)
        .where(FeatureFlag.company_id == company_id)
        .order_by(FeatureFlag.name.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("", response_model=None, status_code=201)
def upsert_feature_flag(
    payload: FeatureFlagIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create or upsert a feature flag (admin only)."""
    company_id = _tenant_id(request)
    _, display = _user_identity(user)

    existing = _get_flag(db, company_id, payload.name)
    is_new = existing is None

    if existing is None:
        flag = FeatureFlag(
            id=uuid4(),
            company_id=company_id,
            name=payload.name,
            enabled=payload.enabled,
            description=payload.description,
            updated_at=utcnow(),
            updated_by=display,
        )
        db.add(flag)
    else:
        existing.enabled = payload.enabled
        if payload.description is not None:
            existing.description = payload.description
        existing.updated_at = utcnow()
        existing.updated_by = display
        flag = existing

    db.commit()
    db.refresh(flag)

    _audit(
        db,
        request,
        user,
        action="feature_flag_created" if is_new else "feature_flag_updated",
        entity_id=str(flag.id),
        details={"name": flag.name, "enabled": bool(flag.enabled)},
    )
    return _serialize(flag)


def _set_enabled(
    name: str,
    value: bool,
    request: Request,
    user: dict,
    db: Session,
) -> dict[str, Any]:
    company_id = _tenant_id(request)
    flag = _get_flag(db, company_id, name)
    if flag is None:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    _, display = _user_identity(user)
    flag.enabled = value
    flag.updated_at = utcnow()
    flag.updated_by = display
    db.commit()
    db.refresh(flag)
    _audit(
        db,
        request,
        user,
        action="feature_flag_enabled" if value else "feature_flag_disabled",
        entity_id=str(flag.id),
        details={"name": flag.name, "enabled": value},
    )
    return _serialize(flag)


@router.post("/{name}/enable", response_model=None)
def enable_feature_flag(
    name: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enable a feature flag (admin only)."""
    return _set_enabled(name, True, request, user, db)


@router.post("/{name}/disable", response_model=None)
def disable_feature_flag(
    name: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Disable a feature flag (admin only)."""
    return _set_enabled(name, False, request, user, db)


@router.delete("/{name}", response_model=None, status_code=204)
def delete_feature_flag(
    name: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a feature flag (admin only)."""
    company_id = _tenant_id(request)
    flag = _get_flag(db, company_id, name)
    if flag is None:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    flag_id = str(flag.id)
    flag_name = flag.name
    db.delete(flag)
    db.commit()
    _audit(
        db,
        request,
        user,
        action="feature_flag_deleted",
        entity_id=flag_id,
        details={"name": flag_name},
    )
    return None
