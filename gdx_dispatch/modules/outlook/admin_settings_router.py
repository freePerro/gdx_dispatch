"""Sprint Outlook Integration — Phase 8 admin settings router.

``GET /api/admin/outlook-settings`` and ``PATCH`` for the tenant admin to
configure: backfill_days, tag-strategy order/enabled/threshold, visibility
rules, vendor_bill_sender_allowlist. (auto_email_triggers retired 2026-08-31.) Mirrors
``admin_ai_settings`` shape (Sprint 1.x S26): module-level dependency callables
for test override, never returns secrets, audit-logged on change.
(The audit half of that sentence was aspirational until #558: the module
contained no audit call at all. Every mutating handler below now stages an
``audit_logs`` row inside its own transaction — the two credential handlers
through ``audit_or_rollback``, so an audit failure takes the credential
change down with it.)

``vendor_bill_sender_allowlist`` was writable only by hand-written SQL until
2026-07-28 — the column existed and gated the whole vendor-bill/statement
intake feature, but no endpoint read or wrote it, so turning the feature on
required someone with database access. It is here now.

Tenant Entra app credentials (client_id, client_secret) live in
``TenantSettings`` and are managed via a separate endpoint
(slice S39 backend, also here below: ``/api/admin/outlook-credentials``).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.audit import (
    audit_or_rollback,
    ensure_audit_table,
    log_audit_event_sync,
    resolve_audit_actor,
)
from gdx_dispatch.core.tenant_settings import TenantSettings
from gdx_dispatch.modules.outlook import key_storage
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookSettings
from gdx_dispatch.modules.outlook.vendor_bill_ingest import normalize_allowlist
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger("gdx_dispatch.modules.outlook.admin_settings_router")

router = APIRouter(
    prefix="/api/admin/outlook",
    tags=["admin", "outlook"],
)


# ── Pydantic shapes ────────────────────────────────────────────────────


# A vendor-bill allowlist entry is either a full address (ar@vendor.com) or a
# bare domain (vendor.com, matched on the domain and its subdomains). Requiring
# a dot in the domain is what stops a typo like "vendor" or a bare TLD from
# becoming a rule that matches far more mail than intended.
_ALLOWLIST_ENTRY = re.compile(
    r"^(?:[^@\s]+@)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)
MAX_ALLOWLIST_ENTRIES = 50


def _clean_allowlist(raw: list[str]) -> list[str]:
    """Normalize + validate allowlisted senders, preserving order.

    This list decides whose attachments GDX downloads and files automatically,
    so a bad entry is not a cosmetic problem — it's either silent non-ingest
    (nothing matches) or over-collection (too much matches). Both are worth a
    422 at the door rather than a debugging session later.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        entry = str(item).strip().lower()
        if not entry:
            continue  # blank chips from the UI are dropped, not an error
        if len(entry) > 320:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"allowlist entry too long: {entry[:60]}…",
            )
        if not _ALLOWLIST_ENTRY.match(entry):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"'{item}' is not a valid sender — use a full address "
                    "(ar@vendor.com) or a domain (vendor.com)"
                ),
            )
        if entry in seen:
            continue
        seen.add(entry)
        cleaned.append(entry)
    if len(cleaned) > MAX_ALLOWLIST_ENTRIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"at most {MAX_ALLOWLIST_ENTRIES} allowlisted senders",
        )
    return cleaned


class OutlookSettingsOut(BaseModel):
    backfill_days: int
    tag_strategy_order: list[str]
    tag_strategy_enabled: dict[str, bool]
    ai_tag_threshold: float
    visibility_rules: dict[str, Any]
    # Senders whose PDF attachments are auto-filed as vendor bills/statements.
    # Empty = the whole vendor-bill intake feature is off.
    vendor_bill_sender_allowlist: list[str] = []


class OutlookSettingsPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backfill_days: int | None = Field(default=None, ge=1, le=3650)
    tag_strategy_order: list[str] | None = None
    tag_strategy_enabled: dict[str, bool] | None = None
    ai_tag_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    visibility_rules: dict[str, Any] | None = None
    vendor_bill_sender_allowlist: list[str] | None = None


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
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


def get_db_for_admin(db: Session = Depends(get_db)) -> Session:
    """The admin session, with the audit table already initialized.

    ``ensure_audit_table`` runs HERE, before the handler stages anything. That
    matters for atomicity, not tidiness: the first audit write for an engine
    COMMITS the guard DDL, and if that fires from inside the audit write
    itself it commits whatever the handler has already staged — the mutation
    lands early and its audit row is no longer bound to it. Every later call
    for that engine is a no-op.

    Deliberately ``Depends(get_db)`` and NOT ``Depends(audit_ready_db)``,
    which looks like it does exactly this and is a trap: that dependency
    resolves its own session via ``_get_db_dep``, which *calls* ``get_db()``
    imperatively instead of declaring ``Depends(get_db)``, so it bypasses
    every ``app.dependency_overrides[get_db]`` in the suite.
    ``routers/customers.py`` records two tests that went 404 that way, and
    switching routers to it during #558 silently pointed two more at the REAL
    database while they still passed green.
    """
    ensure_audit_table(db)
    return db


def _audit(
    db: Session,
    request: Request,
    user: dict[str, Any],
    *,
    action: str,
    entity_type: str,
    entity_id: Any,
    details: dict[str, Any] | None = None,
) -> None:
    """Invariant #1: every create/update/delete here answers who / what / when.

    Actor comes from ``resolve_audit_actor(user, request)``, NOT from
    ``user.get("sub")``. The login JWT this app mints is exactly
    ``{"user_id", "tenant_id", "role"}`` — there is no ``sub`` claim — so a
    ``.get("sub")``-first chain writes "system" for a real admin (#701).

    Call this BEFORE ``db.commit()``: the row is flushed into the caller's
    transaction so the change and its trail commit together.
    """
    log_audit_event_sync(
        db,
        # From the claim, not from entity_id — they happen to be the same value
        # for the one current caller, and a future caller auditing something
        # that is not the tenant would otherwise mislabel the row's tenant.
        tenant_id=str(user.get("tenant_id") or "") or None,
        user_id=resolve_audit_actor(user, request),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=details or {},
        request=request,
    )


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

    Commits the bootstrap INSERT itself. It used to only ``flush()``, which
    meant a caller that never commits — ``get_settings`` — discarded the row on
    session close: reading the settings page a hundred times still left the
    table empty, and only a PATCH ever created it. That made ``outlook_settings``
    look unconfigured to every background task that reads it directly
    (``tdb.get(OutlookSettings, 1)`` returns None), which is a confusing state
    to debug from. Creating the row is idempotent and carries no user data —
    committing it is safe.
    """
    from sqlalchemy.exc import IntegrityError
    row = tenant_db.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    if row is not None:
        return row
    row = OutlookSettings()
    row.id = 1
    tenant_db.add(row)
    try:
        tenant_db.commit()
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

@router.get("/settings", response_model=OutlookSettingsOut)
def get_settings(
    user: dict[str, Any] = Depends(get_admin_principal),
    tenant_db: Session = Depends(get_db_for_admin),
) -> OutlookSettingsOut:
    row = _ensure_settings_row(tenant_db)
    # Seed defaults INTO empty JSON columns so the Vue Settings page can
    # safely v-model nested keys without crashing on undefined nested objects.
    visibility = row.visibility_rules if row.visibility_rules else dict(_DEFAULT_VISIBILITY_RULES)
    return OutlookSettingsOut(
        backfill_days=row.backfill_days or 90,
        tag_strategy_order=row.tag_strategy_order or ["auto_match", "job_thread", "ai"],
        tag_strategy_enabled=row.tag_strategy_enabled or {
            "auto_match": True, "job_thread": True, "ai": True,
        },
        ai_tag_threshold=float(row.ai_tag_threshold or Decimal("0.85")),
        visibility_rules=visibility,
        vendor_bill_sender_allowlist=normalize_allowlist(
            row.vendor_bill_sender_allowlist
        ),
    )


@router.patch("/settings", response_model=OutlookSettingsOut)
def patch_settings(
    payload: OutlookSettingsPatchIn,
    request: Request,
    user: dict[str, Any] = Depends(get_admin_principal),
    tenant_db: Session = Depends(get_db_for_admin),
) -> OutlookSettingsOut:
    row = _ensure_settings_row(tenant_db)

    # Validate BEFORE touching the row. _clean_allowlist raises 422, and doing
    # that after the other fields were already assigned would leave a rejected
    # request's mutations sitting on a live ORM object — harmless only because
    # nothing commits on that path, which is a thin thing to rely on.
    cleaned_allowlist = (
        _clean_allowlist(payload.vendor_bill_sender_allowlist)
        if payload.vendor_bill_sender_allowlist is not None
        else None
    )

    # Snapshot BEFORE assigning. "who set the AI tag threshold to 0.4" is
    # not answerable without "from 0.85" — the old value is the forensic half.
    before_fields = {
        "backfill_days": row.backfill_days,
        "tag_strategy_order": row.tag_strategy_order,
        "tag_strategy_enabled": row.tag_strategy_enabled,
        # Decimal is not JSON; float() here so the diff compares like with like
        # rather than "0.85" (string) vs 0.4 (number).
        "ai_tag_threshold": float(row.ai_tag_threshold) if row.ai_tag_threshold is not None else None,
        "visibility_rules": row.visibility_rules,
    }
    after_fields = dict(before_fields)

    if payload.backfill_days is not None:
        row.backfill_days = payload.backfill_days
        after_fields["backfill_days"] = payload.backfill_days
    if payload.tag_strategy_order is not None:
        row.tag_strategy_order = payload.tag_strategy_order
        after_fields["tag_strategy_order"] = payload.tag_strategy_order
    if payload.tag_strategy_enabled is not None:
        row.tag_strategy_enabled = payload.tag_strategy_enabled
        after_fields["tag_strategy_enabled"] = payload.tag_strategy_enabled
    if payload.ai_tag_threshold is not None:
        row.ai_tag_threshold = Decimal(str(payload.ai_tag_threshold))
        after_fields["ai_tag_threshold"] = float(payload.ai_tag_threshold)
    if payload.visibility_rules is not None:
        row.visibility_rules = payload.visibility_rules
        after_fields["visibility_rules"] = payload.visibility_rules

    allowlist_change: tuple[list[str], list[str]] | None = None
    if cleaned_allowlist is not None:
        before = normalize_allowlist(row.vendor_bill_sender_allowlist)
        if cleaned_allowlist != before:
            allowlist_change = (before, cleaned_allowlist)
        row.vendor_bill_sender_allowlist = cleaned_allowlist
        before_fields["vendor_bill_sender_allowlist"] = before
        after_fields["vendor_bill_sender_allowlist"] = cleaned_allowlist

    tenant_id = _coerce_tenant_uuid(user)
    # Staged BEFORE the commit so the settings change and its trail land
    # together — a row written after `tenant_db.commit()` is discarded when
    # get_db() closes the session without committing (#700).
    changed = {
        k: {"from": v, "to": after_fields[k]}
        for k, v in before_fields.items()
        if after_fields[k] != v
    }
    _audit(
        tenant_db, request, user,
        action="outlook_settings_updated",
        entity_type="outlook_settings",
        entity_id=tenant_id,
        details={"changed": changed},
    )
    tenant_db.commit()
    if allowlist_change is not None:
        # Log this one specifically. It governs whose attachments GDX
        # downloads and files unattended, so "who widened it, and to what"
        # is the question worth being able to answer later.
        before, after = allowlist_change
        log.info(
            "vendor_bill_sender_allowlist changed for tenant %s by %s: %s -> %s",
            tenant_id, user.get("sub") or user.get("user_id") or "?", before, after,
        )
    log.info("outlook settings updated for tenant %s", tenant_id)
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
    request: Request,
    user: dict[str, Any] = Depends(get_admin_principal),
    control_db: Session = Depends(get_db_for_admin),
) -> OutlookCredentialsOut:
    tenant_id = _coerce_tenant_uuid(user)
    settings = control_db.get(TenantSettings, tenant_id)
    if settings is None:
        settings = TenantSettings()
        settings.tenant_id = tenant_id
        control_db.add(settings)

    secret_was_set = bool(getattr(settings, "outlook_client_secret_enc", None))
    if payload.microsoft_tenant_id is not None:
        settings.outlook_microsoft_tenant_id = payload.microsoft_tenant_id
    if payload.client_id is not None:
        settings.outlook_client_id = payload.client_id
    if payload.client_secret is not None:
        # Fernet-encrypt + stamp set_at
        key_storage.set_client_secret(control_db, tenant_id, payload.client_secret)
    # audit_or_rollback, not a bare log call: this writes the Entra client
    # secret — the credential that grants GDX access to the whole mailbox.
    # core/audit.py names "credential stores" as exactly the class where an
    # unaudited change is worse than a failed one, so a failed audit write
    # takes the credential change down with it. Staged BEFORE the commit,
    # which is what makes that rollback able to undo anything.
    #
    # NEVER the secret itself in details — booleans only. This row is readable
    # by anyone who can read the audit feed.
    audit_or_rollback(
        control_db,
        action="outlook_credentials_updated",
        entity_type="outlook_credentials",
        entity_id=str(tenant_id),
        actor=user,
        request=request,
        details={
            "microsoft_tenant_id_set": payload.microsoft_tenant_id is not None,
            "client_id_set": payload.client_id is not None,
            "secret_rotated": payload.client_secret is not None,
            "secret_was_set": secret_was_set,
        },
    )
    control_db.commit()
    log.info("outlook credentials updated for tenant %s", tenant_id)
    return get_credentials(user=user, control_db=control_db)


@router.delete("/credentials", status_code=status.HTTP_204_NO_CONTENT)
def delete_credentials(
    request: Request,
    user: dict[str, Any] = Depends(get_admin_principal),
    control_db: Session = Depends(get_db_for_admin),
) -> None:
    """Wipe the Entra app client_secret (e.g., before rotation)."""
    tenant_id = _coerce_tenant_uuid(user)
    # Read the prior state BEFORE the clear — afterwards there is nothing left
    # to say whether this call revoked a live credential or was a no-op.
    prior = control_db.get(TenantSettings, tenant_id)
    secret_was_set = bool(getattr(prior, "outlook_client_secret_enc", None))
    key_storage.clear_client_secret(control_db, tenant_id)
    # Same reasoning as patch_credentials: revoking mailbox access is a
    # credential-store mutation, so the audit row is a precondition of the
    # change, not a note about it.
    audit_or_rollback(
        control_db,
        action="outlook_credentials_cleared",
        entity_type="outlook_credentials",
        entity_id=str(tenant_id),
        actor=user,
        request=request,
        details={"secret_was_set": secret_was_set},
    )
    control_db.commit()
    log.info("outlook credentials cleared for tenant %s", tenant_id)
    return None


# ── /api/admin/outlook/vendor-bills/sweep ──────────────────────────────


class VendorBillSweepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(default=365, ge=1, le=3650)


class VendorBillSweepOut(BaseModel):
    queued: list[dict[str, str]]
    days: int


@router.post(
    "/vendor-bills/sweep",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=VendorBillSweepOut,
)
def trigger_vendor_bill_sweep(
    payload: VendorBillSweepIn | None = None,
    user: dict[str, Any] = Depends(get_admin_principal),
    db: Session = Depends(get_db_for_admin),
) -> VendorBillSweepOut:
    """Queue the repeatable vendor-bill history sweep (Phase 2, D3) for every
    connected Outlook account. Per-run download/message budgets live in the
    task; the admin re-triggers until the report's ``cap_hit`` is false.
    Guarded: refuses when the sender allowlist is empty (feature off) so the
    button can't silently no-op."""
    tenant_id = _coerce_tenant_uuid(user)
    settings = db.get(OutlookSettings, 1)
    allowlist = normalize_allowlist(
        settings.vendor_bill_sender_allowlist if settings else None
    )
    if not allowlist:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vendor_bill_sender_allowlist is empty — the sweep would ingest nothing. "
                   "Configure allowlisted supplier senders first.",
        )
    accounts = (
        db.query(OutlookAccount)
        .filter(OutlookAccount.refresh_token_enc.isnot(None))
        .all()
    )
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no connected Outlook account",
        )
    days = payload.days if payload is not None else VendorBillSweepIn().days
    from gdx_dispatch.modules.outlook.tasks import sweep_vendor_bill_history

    queued: list[dict[str, str]] = []
    for a in accounts:
        res = sweep_vendor_bill_history.delay(str(a.id), str(tenant_id), days=days)
        queued.append({"account_id": str(a.id), "task_id": str(getattr(res, "id", "") or "")})
    log.info(
        "vendor-bill history sweep queued for %d account(s), days=%d, tenant %s",
        len(queued), days, tenant_id,
    )
    return VendorBillSweepOut(queued=queued, days=days)
