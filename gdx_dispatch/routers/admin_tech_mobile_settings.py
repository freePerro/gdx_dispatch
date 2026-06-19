"""Admin router — per-tenant tech-mobile feature settings.

Sprint tech_mobile S1-Z4 + S1-Z5.

Endpoints:
- GET  /api/admin/feature-settings/tech-mobile
       returns {"catalog": [...], "overrides": {...}, "resolved": {...}}
       — the admin UI uses ``catalog`` to render fields, ``overrides`` to
       show which values the tenant has explicitly set, and ``resolved``
       as the current effective values.
- PUT  /api/admin/feature-settings/tech-mobile
       body: {"key": "tech_mobile.<...>", "value": <Any>}
       — single-key write. The catalog validator enforces type + bounds
       before persistence; an audit row is logged on every successful
       change with action="tech_mobile_settings.changed", details holding
       before/after.
- DELETE /api/admin/feature-settings/tech-mobile/{key}
       — remove an override and let the setting revert to the catalog
       default. Logs an audit row with action="tech_mobile_settings.reset".

Single-key writes (rather than a batch PUT) keep the audit trail at one
row per change — easier to compare before/after when investigating who
turned what off.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.feature_defaults import (
    list_tech_mobile_settings,
    validate_tech_mobile_value,
)
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.core.tenant_mobile_settings import load_tenant_mobile_settings
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers.auth import get_current_user


log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/feature-settings/tech-mobile",
    tags=["admin", "feature-settings", "tech-mobile"],
)


def _tenant_id_from_request(request: Request) -> str:
    """Pull the tenant UUID off request.state. Set by TenantMiddleware."""
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
    return str(getattr(user, "user_id", None) or getattr(user, "id", None) or "unknown")


def _ensure_app_settings(db: Session) -> AppSettings:
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(tenant_mobile_settings={})
        db.add(row)
        db.flush()  # populate row.id without committing yet
    if row.tenant_mobile_settings is None:
        row.tenant_mobile_settings = {}
    return row


class SettingUpdate(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: Any


@router.get("", response_model=None)
def get_tech_mobile_settings(
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    catalog = list_tech_mobile_settings()
    resolved = load_tenant_mobile_settings(db, request=request)
    row = db.query(AppSettings).first()
    overrides = (row.tenant_mobile_settings or {}) if row is not None else {}
    return {
        "catalog": catalog,
        "overrides": overrides,
        "resolved": resolved,
    }


@router.put("", response_model=None)
def put_tech_mobile_setting(
    payload: SettingUpdate,
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # Validator owns type + bounds enforcement; surfaces 400 with the
    # human-readable reason from the catalog spec.
    try:
        coerced = validate_tech_mobile_value(payload.key, payload.value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    row = _ensure_app_settings(db)
    overrides = dict(row.tenant_mobile_settings or {})
    before = overrides.get(payload.key, None)
    overrides[payload.key] = coerced
    row.tenant_mobile_settings = overrides

    # Drop any cached resolved dict so the next read on this request
    # reflects the new override (matters if the caller chains a GET).
    if hasattr(request.state, "mobile_settings"):
        try:
            del request.state.mobile_settings
        except AttributeError:
            pass

    try:
        log_audit_event_sync(
            db,
            tenant_id=_tenant_id_from_request(request),
            user_id=_user_id(user),
            action="tech_mobile_settings.changed",
            entity_type="tenant_mobile_settings",
            entity_id=payload.key,
            details={"key": payload.key, "before": before, "after": coerced},
            request=request,
        )
    except Exception:
        # Audit failures must never silently lose the change OR the audit
        # log entry. Rollback the row write so an admin can retry rather
        # than leaving a settings change unaudited.
        log.exception("tech_mobile_settings_audit_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="audit failure — change rolled back")

    db.commit()
    return {"ok": True, "key": payload.key, "before": before, "after": coerced}


@router.delete("/{key:path}", response_model=None)
def delete_tech_mobile_setting(
    key: str,
    request: Request,
    _: dict = Depends(require_permission("settings.write")),
    user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.query(AppSettings).first()
    if row is None or not row.tenant_mobile_settings:
        return {"ok": True, "key": key, "before": None, "after": None}
    overrides = dict(row.tenant_mobile_settings)
    before = overrides.pop(key, None)
    if before is None:
        return {"ok": True, "key": key, "before": None, "after": None}
    row.tenant_mobile_settings = overrides

    if hasattr(request.state, "mobile_settings"):
        try:
            del request.state.mobile_settings
        except AttributeError:
            pass

    try:
        log_audit_event_sync(
            db,
            tenant_id=_tenant_id_from_request(request),
            user_id=_user_id(user),
            action="tech_mobile_settings.reset",
            entity_type="tenant_mobile_settings",
            entity_id=key,
            details={"key": key, "before": before, "after": None},
            request=request,
        )
    except Exception:
        log.exception("tech_mobile_settings_audit_failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="audit failure — change rolled back")

    db.commit()
    return {"ok": True, "key": key, "before": before, "after": None}
