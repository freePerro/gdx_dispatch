from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Path, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor
from gdx_dispatch.core.branding_logo import BRANDING_LOGO_RE, LOGO_URL_PREFIX
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.cache import invalidate_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import (
    MODULES,
    normalize_module_key,
    require_role,
)
from gdx_dispatch.core.pay_periods import (
    ANCHORED_CADENCES,
    CADENCES,
    invalid_recipient_emails,
    normalize_cadence,
)
from gdx_dispatch.core.payments import stripe_configured
from gdx_dispatch.models.tenant_models import AppSettings, CompanyModuleGrant

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_role("admin", "owner", "superadmin"))],
)

_ALLOWED_INTEGRATIONS = ("quickbooks", "stripe", "twilio", "quickbooks_catalog_sync")
_MODULE_KEY_RE = re.compile(r"^[a-z0-9_-]+$")


def _clean_review_url(value: str | None) -> str | None:
    """Blank clears the review link. Anything else must be an absolute
    http(s) URL: it lands verbatim in an <a href> in every customer email,
    so a javascript: scheme or a relative path is refused at the door rather
    than mailed out. (render_email re-checks the scheme as a second wall.)"""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return ""
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("Review link must be a full URL starting with http:// or https://")
    return value


class SettingsPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    logo: str | None = None
    timezone: str | None = None
    google_review_url: str | None = Field(default=None, max_length=500)
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
    # UI-audit follow-up — operator debug toggle. When ON, handled errors that
    # are normally swallowed are also recorded to the Server Errors log.
    debug_logging_enabled: bool | None = None
    customer_listings_enabled: bool | None = None
    # Email overhaul Phase 4a (locked: automation emails are an on/off
    # OPTION). Default OFF is load-bearing: every workflow action has been a
    # no-op forever, so rules configured in the past must not surprise-send
    # on deploy. The sender user is whose Outlook the automated emails go
    # out as (background sends have no calling user).
    automation_emails_enabled: bool | None = None
    automation_sender_user_id: str | None = Field(default=None, max_length=36)
    # Money-audit M39 (Doug 2026-08-24): payment plans are an on/off OPTION,
    # default OFF here — the endpoint refuses honestly when off.
    payment_plans_enabled: bool | None = None
    # Pay periods (2026-08-26). Cadence values come from core/pay_periods so
    # the API, the arithmetic and the Vue dropdown cannot disagree about
    # what is valid. Cross-field rules (biweekly needs an anchor; autosend
    # needs a recipient) are checked in patch_settings against the MERGED
    # row, not here — a PATCH that sets only one half of a pair is legal on
    # its own and must be judged against what the row will actually hold.
    pay_period_cadence: str | None = None
    pay_period_anchor_start: date | None = None
    pay_period_pay_lag_days: int | None = Field(default=None, ge=0, le=31)
    payroll_recipient_emails: str | None = Field(default=None, max_length=1000)
    payroll_autosend_enabled: bool | None = None
    payroll_autosend_hour: int | None = Field(default=None, ge=0, le=23)

    @field_validator("google_review_url")
    @classmethod
    def _review_url(cls, value: str | None) -> str | None:
        return _clean_review_url(value)


# Keys served by GET /api/settings/branding (300s cache). Either PATCH that
# writes one of these must drop the cached entry, or the Branding tab shows
# the old value for up to five minutes after Save.
_BRANDING_CACHE_KEYS = frozenset({
    "company_name", "logo", "primary_color", "secondary_color",
    "address", "phone", "email", "google_review_url",
})


def _request_tenant_id(request: Request | None) -> str:
    if request is None:
        return ""
    try:
        return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
    except Exception:
        return ""


class BrandingPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = None
    logo: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    google_review_url: str | None = Field(default=None, max_length=500)

    @field_validator("google_review_url")
    @classmethod
    def _review_url(cls, value: str | None) -> str | None:
        return _clean_review_url(value)


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
        "google_review_url": getattr(row, "google_review_url", "") or "",
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
        "debug_logging_enabled": bool(getattr(row, "debug_logging_enabled", False)),
        "customer_listings_enabled": bool(getattr(row, "customer_listings_enabled", False)),
        "automation_emails_enabled": bool(getattr(row, "automation_emails_enabled", False)),
        "payment_plans_enabled": bool(getattr(row, "payment_plans_enabled", False)),
        "automation_sender_user_id": getattr(row, "automation_sender_user_id", None) or "",
        "pay_period_cadence": normalize_cadence(getattr(row, "pay_period_cadence", None)),
        "pay_period_anchor_start": (
            _anchor.isoformat() if (_anchor := getattr(row, "pay_period_anchor_start", None)) else ""
        ),
        "pay_period_pay_lag_days": int(getattr(row, "pay_period_pay_lag_days", 0) or 0),
        "payroll_recipient_emails": getattr(row, "payroll_recipient_emails", None) or "",
        "payroll_autosend_enabled": bool(getattr(row, "payroll_autosend_enabled", False)),
        "payroll_autosend_hour": int(getattr(row, "payroll_autosend_hour", 7) or 0),
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
        "google_review_url": getattr(row, "google_review_url", "") or "",
    }


def _validate_pay_period(row: AppSettings, updates: dict[str, Any]) -> None:
    """Reject a pay-period configuration that cannot produce a real period.

    Judged against the MERGED row — what settings will hold after this
    PATCH — not against the payload alone. Switching cadence to biweekly in
    one request and setting the anchor in the next is a legitimate two-step
    edit only if the first step is refused; otherwise the row spends the
    interval in a state where `period_containing` raises, and the surface
    that raises is a Monday-morning Celery task rather than this screen.
    """
    def merged(key: str, current: Any) -> Any:
        # .get, not `key in updates`: an explicit null in the payload must
        # reach here AS null (that is how the anchor gets cleared), which
        # both forms do — but only this one satisfies the linter.
        return updates.get(key, current)

    cadence = str(merged("pay_period_cadence", getattr(row, "pay_period_cadence", None)) or "")
    if "pay_period_cadence" in updates and cadence not in CADENCES:
        raise HTTPException(
            status_code=422,
            detail=f"pay_period_cadence must be one of: {', '.join(CADENCES)}",
        )

    anchor = merged("pay_period_anchor_start", getattr(row, "pay_period_anchor_start", None))
    if normalize_cadence(cadence) in ANCHORED_CADENCES and not anchor:
        raise HTTPException(
            status_code=422,
            detail=(
                "A two-week pay period needs a start date to count from — "
                "set pay_period_anchor_start to the first day of any period "
                "you have already paid."
            ),
        )

    recipients = merged(
        "payroll_recipient_emails", getattr(row, "payroll_recipient_emails", None)
    )
    bad = invalid_recipient_emails(recipients)
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Not an email address: {', '.join(bad[:5])}",
        )

    # Autosend with nowhere to send is the silent-success shape: the task
    # would run, find no recipient, and do nothing forever while the
    # setting reads ON.
    autosend = merged(
        "payroll_autosend_enabled", getattr(row, "payroll_autosend_enabled", False)
    )
    if autosend and not (str(recipients or "").strip()):
        raise HTTPException(
            status_code=422,
            detail="Turn on automatic sending only after entering who receives it.",
        )

    # ...and somewhere to send FROM. An unattended send has no calling user,
    # and Outlook Graph authenticates as a specific person, so without a
    # nominated sender the scheduled task cannot deliver at all — it would
    # fail every hour while Settings read "on". Caught on prod 2026-08-26.
    if autosend:
        sender = merged(
            "automation_sender_user_id",
            getattr(row, "automation_sender_user_id", None),
        )
        if not str(sender or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "Automatic sending needs a mailbox to send from. Choose "
                    "the sending user under Automated email first."
                ),
            )


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
    _validate_pay_period(row, updates)
    for key in (
        "company_name", "address", "phone", "email", "logo", "timezone",
        "primary_color", "secondary_color", "google_review_url",
        "default_shift_start", "default_shift_end", "default_workdays",
        "qb_accounting_method", "debug_logging_enabled",
        "customer_listings_enabled",
        "automation_emails_enabled", "automation_sender_user_id",
        "payment_plans_enabled",
        "pay_period_cadence", "pay_period_anchor_start",
        "pay_period_pay_lag_days", "payroll_recipient_emails",
        "payroll_autosend_enabled", "payroll_autosend_hour",
    ):
        if key in updates:
            setattr(row, key, updates[key])

    if "integrations" in updates:
        row.integrations = _canonical_integrations(updates["integrations"])

    db.add(row)
    db.commit()
    db.refresh(row)
    # Same cache the Branding PATCH drops — this endpoint writes the same
    # columns and used to leave the 300s settings:branding entry stale.
    if _BRANDING_CACHE_KEYS & updates.keys():
        _tid = _request_tenant_id(request)
        if _tid:
            invalidate_sync(_tid, "settings:branding")
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
        # Stripe is configured by STRIPE_SECRET_KEY on the server, not through
        # this API — there is nothing here to connect or disconnect. Report
        # whether charging actually works so the Settings card can say so
        # instead of guessing. Never returns the key itself.
        "stripe": {"configured": stripe_configured()},
    }


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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    for key in (
        "company_name", "logo", "primary_color", "secondary_color",
        "address", "phone", "email", "google_review_url",
    ):
        if key in updates:
            setattr(row, key, updates[key])

    db.add(row)
    db.commit()
    db.refresh(row)

    # Drop the cached settings:branding entry so the next GET reflects
    # the new values. Without this the 300s TTL on get_branding made
    # PATCH appear to "not stick" until the cache aged out.
    tenant_id = _request_tenant_id(request)
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="patch_branding",
                entity_type="branding",
                entity_id=str(row.id),
                # What changed, not {} — "who/what/when" is invariant #1 and
                # an empty details dict answered only two of the three.
                details=updates,
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_branding_audit_failed')
    return _branding_dict(row)


MAX_LOGO_BYTES = 5 * 1024 * 1024


@router.post("/branding/logo")
def upload_branding_logo(
    request: Request,
    logo: UploadFile = File(...),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Store the company logo and point branding.logo_url at it.

    SettingsView has posted here since branding shipped, but the route never
    existed — the surrounding PATCH succeeded, so "Save" looked fine while the
    logo silently never persisted (contract-gap sweep 2026-07-24, Tier 1.4).
    """
    _require_admin(current_user)
    from gdx_dispatch.routers.uploads import (
        ALLOWED_IMAGE_MIME_TYPES,
        _compress_image,
        _flat_document_path,
        _read_upload_with_limit,
        _write_bytes_to_storage,
    )

    if (logo.content_type or "").strip().lower() not in ALLOWED_IMAGE_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Only jpg/png/webp are supported")
    data = _read_upload_with_limit(logo, MAX_LOGO_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    data, effective_ct = _compress_image(data, logo.content_type or "application/octet-stream")

    ext = "png" if effective_ct == "image/png" else "jpg"
    stored = f"branding-logo-{uuid4().hex}.{ext}"
    _write_bytes_to_storage(_flat_document_path(stored), data)

    row = _ensure_settings(db)
    old_logo = row.logo or ""
    row.logo = f"{LOGO_URL_PREFIX}{stored}"
    db.add(row)
    db.commit()
    db.refresh(row)

    # Best-effort cleanup of the previous upload; only files we minted match.
    old_name = old_logo.rsplit("/", 1)[-1]
    if BRANDING_LOGO_RE.match(old_name):
        try:
            _flat_document_path(old_name).unlink(missing_ok=True)
        except Exception:
            log.warning("branding_logo_cleanup_failed", exc_info=True)

    tenant_id = ""
    try:
        tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
    except Exception:
        tenant_id = ""
    if tenant_id:
        invalidate_sync(tenant_id, "settings:branding")
        try:
            log_audit_event_sync(
                db,
                tenant_id=tenant_id,
                user_id=_actor_id(current_user),
                action="upload_branding_logo",
                entity_type="branding",
                entity_id="",
                details={"filename": stored, "size_bytes": len(data)},
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("upload_branding_logo_audit_failed")
    return _branding_dict(row)
