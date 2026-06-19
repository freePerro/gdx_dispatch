"""Sprint Outlook Integration — Phase 8 admin settings router.

``GET /api/admin/outlook-settings`` and ``PATCH`` for the tenant admin to
configure: backfill_days, tag-strategy order/enabled/threshold, visibility
rules, auto_email_triggers. Mirrors ``admin_ai_settings`` shape (Sprint 1.x
S26): module-level dependency callables for test override, never returns
secrets, audit-logged on change.

Tenant Entra app credentials (client_id, client_secret) live in
``TenantSettings`` (control plane) and are managed via a separate endpoint
(slice S39 backend, also here below: ``/api/admin/outlook-credentials``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantSettings
from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.modules.outlook import key_storage
from gdx_dispatch.modules.outlook.models import OutlookSettings
from gdx_dispatch.routers.auth import get_current_user


log = logging.getLogger("gdx_dispatch.modules.outlook.admin_settings_router")

router = APIRouter(
    prefix="/api/admin/outlook",
    tags=["admin", "outlook"],
)


# ── Pydantic shapes ────────────────────────────────────────────────────


class OutlookSettingsOut(BaseModel):
    backfill_days: int
    tag_strategy_order: list[str]
    tag_strategy_enabled: dict[str, bool]
    ai_tag_threshold: float
    visibility_rules: dict[str, Any]
    auto_email_triggers: dict[str, Any]


class OutlookSettingsPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backfill_days: int | None = Field(default=None, ge=1, le=3650)
    tag_strategy_order: list[str] | None = None
    tag_strategy_enabled: dict[str, bool] | None = None
    ai_tag_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_rules: dict[str, Any] | None = None
    auto_email_triggers: dict[str, Any] | None = None


class OutlookCredentialsOut(BaseModel):
    """NEVER includes the actual secret — only a `secret_set` boolean."""
    microsoft_tenant_id: str | None = None
    client_id: str | None = None
    secret_set: bool
    secret_set_at: str | None = None


class OutlookCredentialsPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    microsoft_tenant_id: str | None = Field(default=None, max_length=64)
    client_id: str | None = Field(default=None, max_length=128)
    client_secret: str | None = Field(default=None, min_length=10, max_length=4000)


# ── auth deps (overridable in tests) ───────────────────────────────────


def get_admin_principal(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    role = (user.get("role") or "").lower()
    if role not in ("admin", "owner", "superadmin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


def get_db_for_admin(db: Session = Depends(get_db)) -> Session:
    return db


def get_db_for_admin(db: Session = Depends(get_db)) -> Session:
    return db


def _coerce_tenant_uuid(user: dict[str, Any]) -> UUID:
    tid = user.get("tenant_id")
    if not tid:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return tid if isinstance(tid, UUID) else UUID(str(tid))


# ── /api/admin/outlook/settings ────────────────────────────────────────


def _ensure_settings_row(tenant_db: Session) -> OutlookSettings:
    """Singleton fetch-or-create. Race-tolerant: two concurrent admin calls
    that both see "no row" will both INSERT id=1; the second hits an
    IntegrityError on the unique PK — caught + recovered by re-fetching.
    """
    from sqlalchemy.exc import IntegrityError
    row = tenant_db.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    if row is not None:
        return row
    row = OutlookSettings()
    row.id = 1
    tenant_db.add(row)
    try:
        tenant_db.flush()
    except IntegrityError:
        tenant_db.rollback()
        row = (
            tenant_db.query(OutlookSettings)
            .filter(OutlookSettings.id == 1)
            .first()
        )
        if row is None:
            raise  # genuine error — re-raise after rollback
    return row


_DEFAULT_VISIBILITY_RULES = {
    "tagged_visibility_above_role": "tech_plus_one",
    "tech_recipient_visible_to_all_techs": True,
    "tech_outbound_no_tag_visibility": "only_sender",
    "tech_to_tech_internal_visibility": "only_participants",
    "above_tech_scope": "all_tagged",
    "untagged_visibility": "only_owner",
}

_DEFAULT_AUTO_EMAIL_TRIGGERS = {
    "invoice.created": {"subject": "", "template": "", "enabled_default": False},
    "job.completed": {"subject": "", "template": "", "enabled_default": False},
    "estimate.sent": {"subject": "", "template": "", "enabled_default": False},
}


@router.get("/settings", response_model=OutlookSettingsOut)
def get_settings(
    user: dict[str, Any] = Depends(get_admin_principal),
    tenant_db: Session = Depends(get_db_for_admin),
) -> OutlookSettingsOut:
    row = _ensure_settings_row(tenant_db)
    # Seed defaults INTO empty JSON columns so the Vue Settings page can
    # safely v-model nested keys without crashing on undefined nested objects.
    visibility = row.visibility_rules if row.visibility_rules else dict(_DEFAULT_VISIBILITY_RULES)
    triggers = row.auto_email_triggers if row.auto_email_triggers else dict(_DEFAULT_AUTO_EMAIL_TRIGGERS)
    return OutlookSettingsOut(
        backfill_days=row.backfill_days or 90,
        tag_strategy_order=row.tag_strategy_order or ["auto_match", "job_thread", "ai"],
        tag_strategy_enabled=row.tag_strategy_enabled or {
            "auto_match": True, "job_thread": True, "ai": True,
        },
        ai_tag_threshold=float(row.ai_tag_threshold or Decimal("0.85")),
        visibility_rules=visibility,
        auto_email_triggers=triggers,
    )


@router.patch("/settings", response_model=OutlookSettingsOut)
def patch_settings(
    payload: OutlookSettingsPatchIn,
    user: dict[str, Any] = Depends(get_admin_principal),
    tenant_db: Session = Depends(get_db_for_admin),
) -> OutlookSettingsOut:
    row = _ensure_settings_row(tenant_db)
    if payload.backfill_days is not None:
        row.backfill_days = payload.backfill_days
    if payload.tag_strategy_order is not None:
        row.tag_strategy_order = payload.tag_strategy_order
    if payload.tag_strategy_enabled is not None:
        row.tag_strategy_enabled = payload.tag_strategy_enabled
    if payload.ai_tag_threshold is not None:
        row.ai_tag_threshold = Decimal(str(payload.ai_tag_threshold))
    if payload.visibility_rules is not None:
        row.visibility_rules = payload.visibility_rules
    if payload.auto_email_triggers is not None:
        row.auto_email_triggers = payload.auto_email_triggers
    tenant_db.commit()
    log.info("outlook settings updated for tenant %s", _coerce_tenant_uuid(user))
    return get_settings(user=user, tenant_db=tenant_db)


# ── /api/admin/outlook/credentials ─────────────────────────────────────


@router.get("/credentials", response_model=OutlookCredentialsOut)
def get_credentials(
    user: dict[str, Any] = Depends(get_admin_principal),
    control_db: Session = Depends(get_db_for_admin),
) -> OutlookCredentialsOut:
    """Returns the public-safe credential state — NEVER the client_secret."""
    tenant_id = _coerce_tenant_uuid(user)
    settings = control_db.get(TenantSettings, tenant_id)
    if settings is None:
        return OutlookCredentialsOut(secret_set=False)
    return OutlookCredentialsOut(
        microsoft_tenant_id=settings.outlook_microsoft_tenant_id,
        client_id=settings.outlook_client_id,
        secret_set=bool(settings.outlook_client_secret_enc),
        secret_set_at=settings.outlook_secret_set_at.isoformat() if settings.outlook_secret_set_at else None,
    )


@router.patch("/credentials", response_model=OutlookCredentialsOut)
def patch_credentials(
    payload: OutlookCredentialsPatchIn,
    user: dict[str, Any] = Depends(get_admin_principal),
    control_db: Session = Depends(get_db_for_admin),
) -> OutlookCredentialsOut:
    tenant_id = _coerce_tenant_uuid(user)
    settings = control_db.get(TenantSettings, tenant_id)
    if settings is None:
        settings = TenantSettings()
        settings.tenant_id = tenant_id
        control_db.add(settings)

    if payload.microsoft_tenant_id is not None:
        settings.outlook_microsoft_tenant_id = payload.microsoft_tenant_id
    if payload.client_id is not None:
        settings.outlook_client_id = payload.client_id
    if payload.client_secret is not None:
        # Fernet-encrypt + stamp set_at
        key_storage.set_client_secret(control_db, tenant_id, payload.client_secret)
    control_db.commit()
    log.info("outlook credentials updated for tenant %s", tenant_id)
    return get_credentials(user=user, control_db=control_db)


@router.delete("/credentials", status_code=status.HTTP_204_NO_CONTENT)
def delete_credentials(
    user: dict[str, Any] = Depends(get_admin_principal),
    control_db: Session = Depends(get_db_for_admin),
) -> None:
    """Wipe the Entra app client_secret (e.g., before rotation)."""
    tenant_id = _coerce_tenant_uuid(user)
    key_storage.clear_client_secret(control_db, tenant_id)
    control_db.commit()
    log.info("outlook credentials cleared for tenant %s", tenant_id)
    return None
