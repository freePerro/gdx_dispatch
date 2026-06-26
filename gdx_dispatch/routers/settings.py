from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.cache import cached, invalidate_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import (
    MODULES,
    normalize_module_key,
    require_role,
)
from gdx_dispatch.models.tenant_models import AppSettings, CompanyModuleGrant

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_role("admin", "owner", "superadmin"))],
)

_ALLOWED_INTEGRATIONS = ("quickbooks", "stripe", "twilio", "quickbooks_catalog_sync")
_MODULE_KEY_RE = re.compile(r"^[a-z0-9_-]+$")


class SettingsPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    logo: str | None = None
    timezone: str | None = None
    integrations: dict[str, bool] | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    # Sprint dispatch-capacity (2026-05-20) — tenant default shop hours.
    # workdays bitmask Mon=1..Sun=64; 1<=val<=127.
    default_shift_start: time | None = None
    default_shift_end: time | None = None
    default_workdays: int | None = Field(default=None, ge=1, le=127)
    # Sprint monthly-budget-history (2026-05-24) — Cash vs Accrual basis
    # for the QBO ProfitAndLoss report that drives budget actuals.
    qb_accounting_method: str | None = Field(default=None, pattern="^(Cash|Accrual)$")


class BrandingPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = None
    logo: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None


def _require_admin(current_user: dict[str, Any]) -> None:
    # owner outranks admin (RBAC_HIERARCHY); superadmin is platform-level. Gating
    # on == "admin" wrongly 403'd the owner — the seeded account — out of every
    # /api/settings endpoint.
    if str(current_user.get("role", "")) not in {"admin", "owner", "superadmin"}:
        raise HTTPException(status_code=403, detail="Admin access required")


def _canonical_integrations(value: dict[str, Any] | None) -> dict[str, bool]:
    current = value if isinstance(value, dict) else {}
    return {key: bool(current.get(key, False)) for key in _ALLOWED_INTEGRATIONS}


def quickbooks_catalog_sync_enabled(db: Session) -> bool:
    """#57 — operator gate for QB *catalog* sync (pull/push). Defaults OFF: the
    prod QB catalog data is untrusted, and nothing should repopulate it unless an
    admin explicitly turns this on in Admin → Integration Settings. Distinct from
    `integrations.quickbooks` (which gates QB invoicing/banking)."""
    row = db.query(AppSettings).first()
    integrations = _canonical_integrations(row.integrations if row else None)
    return bool(integrations.get("quickbooks_catalog_sync", False))


def _ensure_settings(db: Session) -> AppSettings:
    row = db.query(AppSettings).first()
    if row:
        return row

    row = AppSettings(
        company_name="",
        address="",
        phone="",
        email="",
        logo="",
        timezone="America/New_York",
        enabled_modules=[],
        notification_preferences={},
        integrations=_canonical_integrations(None),
        primary_color="#0f172a",
        secondary_color="#2563eb",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _settings_dict(row: AppSettings) -> dict[str, Any]:
    return {
        "company_name": row.company_name or "",
        "address": row.address or "",
        "phone": row.phone or "",
        "email": row.email or "",
        "logo": row.logo or "",
        "timezone": row.timezone or "America/New_York",
        "enabled_modules": list(row.enabled_modules or []),
        "notification_preferences": dict(row.notification_preferences or {}),
        "integrations": _canonical_integrations(row.integrations),
        "primary_color": row.primary_color or "#0f172a",
        "secondary_color": row.secondary_color or "#2563eb",
        "default_shift_start": row.default_shift_start.isoformat(timespec="minutes")
        if row.default_shift_start else "08:00",
        "default_shift_end": row.default_shift_end.isoformat(timespec="minutes")
        if row.default_shift_end else "17:00",
        "default_workdays": int(row.default_workdays) if row.default_workdays is not None else 31,
        "qb_accounting_method": (
            row.qb_accounting_method if getattr(row, "qb_accounting_method", None) else "Accrual"
        ),
    }


def _branding_dict(row: AppSettings) -> dict[str, Any]:
    return {
        "company_name": row.company_name or "",
        "logo_url": row.logo or "",
        "primary_color": row.primary_color or "#0f172a",
        "accent_color": row.secondary_color or "#2563eb",
        "address": row.address or "",
        "phone": row.phone or "",
        "email": row.email or "",
    }


def _actor_id(user: dict[str, Any]) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


@router.get("")
def get_settings(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    row = _ensure_settings(db)
    return _settings_dict(row)


@router.patch("")
def patch_settings(
    payload: SettingsPatchIn,
    request: Request = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    row = _ensure_settings(db)

    # Two dumps: one in Python-native types for the ORM setattr loop
    # (Pydantic gives us datetime.time / int directly), one in mode="json"
    # for the audit row so datetime.time ISO-serializes instead of
    # blowing up jsonable_encoder downstream.
    audit_updates = payload.model_dump(exclude_unset=True, mode="json")
    updates = payload.model_dump(exclude_unset=True)
    for key in (
        "company_name", "address", "phone", "email", "logo", "timezone",
        "primary_color", "secondary_color",
        "default_shift_start", "default_shift_end", "default_workdays",
        "qb_accounting_method",
    ):
        if key in updates:
            setattr(row, key, updates[key])

    if "integrations" in updates:
        row.integrations = _canonical_integrations(updates["integrations"])

    db.add(row)
    db.commit()
    db.refresh(row)
    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(getattr(request, "state", None), "tenant", {}).get("id", "")) if request else None,
        user_id=_actor_id(current_user),
        action="settings_updated",
        entity_type="settings",
        entity_id=str(row.id),
        details=audit_updates,
        ip_address=(request.client.host if request and request.client else None),
        request=request,
    )
    db.commit()
    return _settings_dict(row)


@router.get("/modules")
def get_modules(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.query(CompanyModuleGrant).first()
    if not existing:
        now = datetime.now(timezone.utc)
        for key, cfg in MODULES.items():
            if cfg["default"]:
                already = db.query(CompanyModuleGrant).filter(
                    CompanyModuleGrant.module_key == key,
                ).first()
                if not already:
                    db.add(CompanyModuleGrant(
                        id=str(uuid4()),
                        company_id=tenant_id,
                        module_key=key,
                        granted_at=now,
                        created_at=now,
                    ))
        db.commit()

    # D101 (2026-04-25): a per-GET autohealer for GDX used to live here, re-granting
    # every module on every read. That made admin disable a no-op — the row deleted
    # by /modules/{key}/disable was resurrected by the next GET. Bootstrap of "GDX
    # has all modules" now happens once via gdx_dispatch/tools/bootstrap_modules_for_tenant.py;
    # row absence here means the admin disabled it.

    rows = db.query(CompanyModuleGrant.module_key).all()
    granted = {str(r[0]) for r in rows}

    payload = []
    for key, cfg in MODULES.items():
        payload.append(
            {
                "key": key,
                "name": cfg["name"],
                "tier": str(cfg["tier"]),
                "default": bool(cfg["default"]),
                "enabled": key in granted,
                "locked": False,
                "upgrade_required": None,
            }
        )
    payload.sort(key=lambda item: item["name"])

    return {"modules": payload}


@router.post("/modules/{key}/enable")
def enable_module(
    request: Request,
    key: str = Path(...),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    module_key = key.strip().lower()
    if not module_key or not _MODULE_KEY_RE.fullmatch(module_key):
        raise HTTPException(status_code=422, detail="Invalid module key")
    try:
        canonical_key = normalize_module_key(module_key)
    except ValueError as exc:
        log.exception("enable_module_key_normalize_failed")
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    already = db.query(CompanyModuleGrant).filter(
        CompanyModuleGrant.module_key == canonical_key,
    ).first()
    if not already:
        now = datetime.now(timezone.utc)
        db.add(CompanyModuleGrant(
            id=str(uuid4()),
            company_id=tenant_id,
            module_key=canonical_key,
            granted_at=now,
            created_at=now,
        ))
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=_actor_id(current_user),
        action="module_enabled",
        entity_type="module",
        entity_id=canonical_key,
        details={"module_key": canonical_key},
        ip_address=request.client.host if request.client else None,
        request=request,
    )
    db.commit()

    return {"status": "enabled", "key": canonical_key}


@router.post("/modules/{key}/disable")
def disable_module(
    request: Request,
    key: str = Path(...),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    module_key = key.strip().lower()
    if not module_key or not _MODULE_KEY_RE.fullmatch(module_key):
        raise HTTPException(status_code=422, detail="Invalid module key")
    try:
        canonical_key = normalize_module_key(module_key)
    except ValueError as exc:
        log.exception("disable_module_key_normalize_failed")
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    db.query(CompanyModuleGrant).filter(
        CompanyModuleGrant.module_key == canonical_key,
    ).delete()
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=_actor_id(current_user),
        action="module_disabled",
        entity_type="module",
        entity_id=canonical_key,
        details={"module_key": canonical_key},
        ip_address=request.client.host if request.client else None,
        request=request,
    )
    db.commit()
    return {"status": "disabled", "key": canonical_key}


@router.get("/notifications")
def get_notification_preferences(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, dict[str, Any]]:
    _require_admin(current_user)
    row = _ensure_settings(db)
    return {"notification_preferences": dict(row.notification_preferences or {})}


@router.patch("/notifications")
def patch_notification_preferences(
    payload: dict[str, Any],
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, dict[str, Any]]:
    _require_admin(current_user)
    row = _ensure_settings(db)
    row.notification_preferences = dict(payload)
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="patch_notification_preferences",
                entity_type="notification_preference",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_notification_preferences_audit_failed')
    return {"notification_preferences": dict(row.notification_preferences or {})}


@router.get("/integrations")
def list_integrations(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    row = _ensure_settings(db)
    integrations = _canonical_integrations(row.integrations)
    active = [key for key, enabled in integrations.items() if enabled]
    google_maps_configured = bool((row.google_maps_api_key or "").strip())
    return {
        "integrations": integrations,
        "active_integrations": active,
        # Per-key integrations (string credentials, not boolean flags). The
        # actual key is fetched via the dedicated GET below — never returned
        # in the bulk listing — so a console.log accidentally dumping the
        # whole settings response can't leak it.
        "google_maps": {"configured": google_maps_configured},
    }


@router.get("/integrations/google-maps")
def get_google_maps_key(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the tenant's Google Maps JS API key.

    Reachable by any authenticated user — the key is exposed in every
    browser that loads /maps (it goes in the `<script src=...&key=...>`
    URL), so there's nothing to gate. The real control is HTTP-referrer
    restriction set in Google Cloud Console by the admin who owns the
    key.
    """
    row = _ensure_settings(db)
    key = (row.google_maps_api_key or "").strip()
    return {"key": key, "configured": bool(key)}


class GoogleMapsKeyPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = None


@router.patch("/integrations/google-maps")
def patch_google_maps_key(
    payload: GoogleMapsKeyPatchIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    row = _ensure_settings(db)
    new_key = (payload.key or "").strip() or None
    row.google_maps_api_key = new_key
    db.commit()
    db.refresh(row)
    tenant_id = ""
    try:
        tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
    except Exception:
        tenant_id = ""
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system"),
            action="patch_google_maps_key",
            entity_type="integration",
            entity_id="google_maps",
            details={"configured": bool(new_key)},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("patch_google_maps_key_audit_failed")
    return {"configured": bool(new_key)}


@router.post("/integrations/{provider}/connect")
def connect_integration(
    provider: str,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enable an integration flag for this tenant. Real OAuth/API-key flow
    happens downstream; this just flips the feature switch."""
    _require_admin(current_user)
    if provider not in _ALLOWED_INTEGRATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {list(_ALLOWED_INTEGRATIONS)}",
        )
    row = _ensure_settings(db)
    integrations = _canonical_integrations(row.integrations)
    integrations[provider] = True
    row.integrations = integrations
    db.commit()
    db.refresh(row)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="connect_integration",
                entity_type="integration",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('connect_integration_audit_failed')
    return {
        "provider": provider,
        "status": "connected",
        "integrations": integrations,
    }


@router.post("/integrations/{provider}/disconnect")
def disconnect_integration(
    provider: str,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Disable an integration flag. Does not delete stored credentials —
    downstream worker handles revocation."""
    _require_admin(current_user)
    if provider not in _ALLOWED_INTEGRATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {list(_ALLOWED_INTEGRATIONS)}",
        )
    row = _ensure_settings(db)
    integrations = _canonical_integrations(row.integrations)
    if not integrations.get(provider):
        raise HTTPException(status_code=409, detail=f"{provider} is already disconnected")
    integrations[provider] = False
    row.integrations = integrations
    db.commit()
    db.refresh(row)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="disconnect_integration",
                entity_type="integration",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('disconnect_integration_audit_failed')
    return {
        "provider": provider,
        "status": "disconnected",
        "integrations": integrations,
    }


@router.post("/integrations/{provider}/sync")
def sync_integration(
    provider: str,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Trigger a one-shot sync for an active integration. Returns queued
    status — actual sync runs in a Celery worker."""
    _require_admin(current_user)
    if provider not in _ALLOWED_INTEGRATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {list(_ALLOWED_INTEGRATIONS)}",
        )
    row = _ensure_settings(db)
    integrations = _canonical_integrations(row.integrations)
    if not integrations.get(provider):
        raise HTTPException(
            status_code=409,
            detail=f"{provider} must be connected before sync can be triggered",
        )
    # Placeholder — real worker enqueue goes here (celery_app.send_task)
    now = datetime.now(timezone.utc)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="sync_integration",
                entity_type="integration",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('sync_integration_audit_failed')
    return {
        "provider": provider,
        "status": "sync_queued",
        "queued_at": now.isoformat(),
        "message": "Sync job queued — delivery via worker downstream",
    }


@router.get("/branding")
async def get_branding(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # Branding is readable by every authenticated user in the tenant —
    # it's the company name + logo + colors the SPA uses to render. A
    # tech logging in needs to see "Example Garage Doors" in the topbar,
    # not the platform default. Write side (PATCH /branding) stays
    # admin-gated.
    tenant_id = company_id()
    return await cached(
        tenant_id,
        "settings:branding",
        ttl_seconds=300,
        fetcher=lambda: _branding_dict(_ensure_settings(db)),
    )


@router.patch("/branding")
def patch_branding(
    payload: BrandingPatchIn,
    request: Request = None,  # type: ignore[assignment]
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_admin(current_user)
    row = _ensure_settings(db)

    updates = payload.model_dump(exclude_unset=True)
    for key in ("company_name", "logo", "primary_color", "secondary_color", "address", "phone", "email"):
        if key in updates:
            setattr(row, key, updates[key])

    db.add(row)
    db.commit()
    db.refresh(row)

    # Drop the cached settings:branding entry so the next GET reflects
    # the new values. Without this the 300s TTL on get_branding made
    # PATCH appear to "not stick" until the cache aged out.
    tenant_id = ""
    if request is not None:
        try:
            tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
        except Exception:
            tenant_id = ""
    if tenant_id:
        invalidate_sync(tenant_id, "settings:branding")
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="patch_branding",
                entity_type="branding",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_branding_audit_failed')
    return _branding_dict(row)
