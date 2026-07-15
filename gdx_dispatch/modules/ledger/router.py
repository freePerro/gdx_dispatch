"""Accounting Settings API (S4.5, plan §S4.5 / guiding rule 3).

The browser home for every CPA-dependent choice: the seeded CoA, the
role/account maps, policy toggles, and the ``ledger_posting_enabled`` master
switch. CPA answers arrive as data entry here — never as a deploy.

Guardrails (spec §7 parity with QBO's "can't change accounting method after
transactions"): ``inventory_treatment``, ``cutover_month``, and
``payment_method_role_map`` go read-only once posting is enabled or any
journal entry exists. System accounts can be renamed/renumbered but never
deleted, deactivated, or role-reassigned.
"""
from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.modules.ledger.coa import LedgerConfigError
from gdx_dispatch.modules.ledger.models import (
    ACCOUNT_TYPES,
    ALL_ROLES,
    GlAccount,
    GlJournalEntry,
)
from gdx_dispatch.modules.ledger.service import (
    ensure_gl_seed,
    get_gl_settings,
    validate_payment_method_map,
    validate_role_map,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounting", tags=["accounting"])

ENABLE_CONFIRM_PHRASE = "ENABLE LEDGER POSTING"

# Fields that freeze once the ledger is live (flag on OR entries exist).
LOCKED_ONCE_LIVE = ("inventory_treatment", "cutover_month", "payment_method_role_map")

_BASES = ("cash", "accrual")
_INVENTORY_TREATMENTS = ("expense", "capitalize")


def _tenant_id(user: dict) -> str:
    return str(user.get("tenant_id") or user.get("company_id") or "")


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _entries_exist(db: Session, company_id: str) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(GlJournalEntry)
            .where(GlJournalEntry.company_id == company_id)
        )
    )


def _locked_fields(db: Session, settings, company_id: str) -> dict[str, str]:
    if settings.ledger_posting_enabled:
        reason = "ledger posting is enabled"
    elif _entries_exist(db, company_id):
        reason = "journal entries have been posted"
    else:
        return {}
    return {f: reason for f in LOCKED_ONCE_LIVE}


def _audit(db: Session, company_id: str, actor: str, action: str, details: dict) -> None:
    db.add(
        AuditLog(
            tenant_id=company_id,
            user_id=actor,
            action=action,
            entity_type="gl_settings",
            details=details,
        )
    )


def _account_payload(a: GlAccount) -> dict:
    return {
        "id": str(a.id),
        "code": a.code,
        "name": a.name,
        "type": a.type,
        "role": a.role,
        "is_system": a.is_system,
        "active": a.active,
    }


def _settings_payload(db: Session, settings, company_id: str) -> dict:
    accounts = db.scalars(
        select(GlAccount)
        .where(GlAccount.company_id == company_id)
        .order_by(GlAccount.code)
    ).all()
    return {
        "seeded": True,
        "settings": {
            "ledger_posting_enabled": settings.ledger_posting_enabled,
            "reporting_basis": settings.reporting_basis,
            "tax_basis": settings.tax_basis,
            "inventory_treatment": settings.inventory_treatment,
            "cutover_month": settings.cutover_month.isoformat() if settings.cutover_month else None,
            "entity_type": settings.entity_type,
            "opening_bank_attested_at": (
                settings.opening_bank_attested_at.isoformat()
                if settings.opening_bank_attested_at
                else None
            ),
            "opening_bank_attested_by": settings.opening_bank_attested_by,
            "payment_method_role_map": settings.payment_method_role_map,
            "credit_reason_role_map": settings.credit_reason_role_map,
            "expense_category_account_map": settings.expense_category_account_map,
            "revenue_category_account_map": settings.revenue_category_account_map or {},
            "cpa_review": settings.cpa_review or {},
        },
        "accounts": [_account_payload(a) for a in accounts],
        "roles": list(ALL_ROLES),
        "account_types": list(ACCOUNT_TYPES),
        "locked_fields": _locked_fields(db, settings, company_id),
        "enable_confirm_phrase": ENABLE_CONFIRM_PHRASE,
        "entries_exist": _entries_exist(db, company_id),
    }


@router.get("/settings")
def get_accounting_settings(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.read")),
) -> dict:
    """Pure read (audit round 1: the read path must not be a writer — a
    read-only viewer's first page view was seeding the CoA). Unseeded →
    ``seeded: false``; the page calls POST /settings/initialize (write-gated)
    to materialize."""
    company_id = _tenant_id(user)
    settings = get_gl_settings(db, company_id)
    if settings is None:
        return {
            "seeded": False,
            "settings": None,
            "accounts": [],
            "roles": list(ALL_ROLES),
            "account_types": list(ACCOUNT_TYPES),
            "locked_fields": {},
            "enable_confirm_phrase": ENABLE_CONFIRM_PHRASE,
            "entries_exist": _entries_exist(db, company_id),
        }
    return _settings_payload(db, settings, company_id)


@router.post("/settings/initialize")
def initialize_accounting(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    """Idempotent first-boot: seed the starter CoA + materialize gl_settings
    (and top up any default keys a release added). Audited — config must
    never mutate actorlessly."""
    company_id = _tenant_id(user)
    already = get_gl_settings(db, company_id) is not None
    settings = ensure_gl_seed(db, company_id)
    if not already:
        _audit(db, company_id, _actor(user), "gl_settings_initialized", {})
    try:
        db.commit()
    except IntegrityError:
        # Two racing first-initializes: the unique role index kills one seed.
        # Self-heals — re-read the winner's rows.
        db.rollback()
        settings = ensure_gl_seed(db, company_id)
        db.commit()
    return _settings_payload(db, settings, company_id)


class SettingsPatchIn(BaseModel):
    reporting_basis: str | None = None
    tax_basis: str | None = None
    inventory_treatment: str | None = None
    cutover_month: date | None = None
    clear_cutover_month: bool = False
    entity_type: str | None = Field(default=None, max_length=40)
    clear_entity_type: bool = False
    payment_method_role_map: dict[str, str] | None = None
    credit_reason_role_map: dict[str, str] | None = None
    expense_category_account_map: dict[str, str] | None = None
    revenue_category_account_map: dict[str, str] | None = None
    attest_opening_bank: bool = False
    clear_opening_bank_attestation: bool = False


@router.patch("/settings")
def patch_accounting_settings(
    payload: SettingsPatchIn,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    company_id = _tenant_id(user)
    actor = _actor(user)
    settings = get_gl_settings(db, company_id)
    if settings is None:
        raise HTTPException(status_code=409, detail="accounting not initialized — POST /api/accounting/settings/initialize first")
    before_maps = (settings.payment_method_role_map, settings.credit_reason_role_map)
    settings = ensure_gl_seed(db, company_id)  # key top-up (release migration)
    if (settings.payment_method_role_map, settings.credit_reason_role_map) != before_maps:
        # Top-up touched config (possibly locked fields) — that mutation must
        # never be actorless/unaudited (audit round 1).
        _audit(db, company_id, "system:release-topup", "gl_settings_topped_up", {})
    locked = _locked_fields(db, settings, company_id)

    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    for flag in ("clear_cutover_month", "attest_opening_bank", "clear_entity_type", "clear_opening_bank_attestation"):
        updates.pop(flag, None)
    if payload.clear_cutover_month:
        updates["cutover_month"] = None
    if payload.clear_entity_type:
        updates["entity_type"] = None

    for field in updates:
        if field in locked:
            raise HTTPException(
                status_code=409,
                detail=f"{field} is locked: {locked[field]} (QBO-parity guardrail)",
            )

    if "reporting_basis" in updates and updates["reporting_basis"] not in _BASES:
        raise HTTPException(status_code=422, detail=f"reporting_basis must be one of {_BASES}")
    if "tax_basis" in updates and updates["tax_basis"] not in _BASES:
        raise HTTPException(status_code=422, detail=f"tax_basis must be one of {_BASES}")
    if "inventory_treatment" in updates and updates["inventory_treatment"] not in _INVENTORY_TREATMENTS:
        raise HTTPException(
            status_code=422, detail=f"inventory_treatment must be one of {_INVENTORY_TREATMENTS}"
        )
    if updates.get("cutover_month"):
        updates["cutover_month"] = updates["cutover_month"].replace(day=1)

    try:
        if "payment_method_role_map" in updates:
            validate_payment_method_map(updates["payment_method_role_map"])
        if "credit_reason_role_map" in updates:
            validate_role_map("credit_reason_role_map", updates["credit_reason_role_map"])
    except LedgerConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for map_field, wanted_type in (
        ("expense_category_account_map", "expense"),
        ("revenue_category_account_map", "revenue"),
    ):
        if map_field not in updates:
            continue
        # Mapped accounts must exist, be ACTIVE, and carry the right type — a
        # map to 1200 AR would have posting debit/credit the wrong side of
        # the books (audit round 1: existence alone was checked).
        accounts_by_id = {
            str(a.id): a
            for a in db.scalars(
                select(GlAccount).where(GlAccount.company_id == company_id)
            )
        }
        bad = {}
        for cat, acct_id in updates[map_field].items():
            acct = accounts_by_id.get(acct_id)
            if acct is None:
                bad[cat] = "unknown account"
            elif not acct.active:
                bad[cat] = f"{acct.code} {acct.name} is deactivated"
            elif acct.type != wanted_type:
                bad[cat] = f"{acct.code} {acct.name} is {acct.type}, not {wanted_type}"
        if bad:
            raise HTTPException(status_code=422, detail=f"{map_field} invalid: {bad}")

    for field, value in updates.items():
        setattr(settings, field, value)

    if payload.attest_opening_bank:
        settings.opening_bank_attested_at = utcnow()
        settings.opening_bank_attested_by = actor
    if payload.clear_opening_bank_attestation:
        settings.opening_bank_attested_at = None
        settings.opening_bank_attested_by = None

    _audit(
        db, company_id, actor, "gl_settings_updated",
        {"fields": sorted(updates.keys()) + (["opening_bank_attested"] if payload.attest_opening_bank else [])},
    )
    db.commit()
    return _settings_payload(db, settings, company_id)


class CpaReviewIn(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=50)


@router.post("/settings/cpa-review")
def stamp_cpa_review(
    payload: CpaReviewIn,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    company_id = _tenant_id(user)
    actor = _actor(user)
    settings = ensure_gl_seed(db, company_id)
    stamps = dict(settings.cpa_review or {})
    now = utcnow().isoformat()
    for key in payload.keys:
        stamps[key] = {"reviewed_at": now, "by": actor}
    settings.cpa_review = stamps
    _audit(db, company_id, actor, "gl_cpa_review_stamped", {"keys": payload.keys})
    db.commit()
    return _settings_payload(db, settings, company_id)


class EnablePostingIn(BaseModel):
    confirm: str


@router.post("/settings/enable-posting")
def enable_posting(
    payload: EnablePostingIn,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    """The one-way door. Requires the exact confirm phrase; locks the
    guardrailed fields from this moment on."""
    company_id = _tenant_id(user)
    settings = ensure_gl_seed(db, company_id)
    if payload.confirm != ENABLE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=422,
            detail=f'type "{ENABLE_CONFIRM_PHRASE}" exactly to enable posting',
        )
    if not settings.ledger_posting_enabled:
        settings.ledger_posting_enabled = True
        _audit(db, company_id, _actor(user), "gl_posting_enabled", {})
        db.commit()
    return _settings_payload(db, settings, company_id)


@router.post("/settings/disable-posting")
def disable_posting(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    """The abort path — only while the ledger is still empty. Once entries
    exist the door has closed (books can't silently stop being the truth)."""
    company_id = _tenant_id(user)
    settings = ensure_gl_seed(db, company_id)
    if _entries_exist(db, company_id):
        raise HTTPException(
            status_code=409,
            detail="journal entries exist — posting can no longer be disabled",
        )
    if settings.ledger_posting_enabled:
        settings.ledger_posting_enabled = False
        _audit(db, company_id, _actor(user), "gl_posting_disabled", {})
        db.commit()
    return _settings_payload(db, settings, company_id)


class AccountCreateIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    type: str

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class AccountPatchIn(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=20)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    active: bool | None = None

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


@router.post("/accounts", status_code=201)
def create_account(
    payload: AccountCreateIn,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    company_id = _tenant_id(user)
    if payload.type not in ACCOUNT_TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of {ACCOUNT_TYPES}")
    clash = db.scalars(
        select(GlAccount).where(
            GlAccount.company_id == company_id,
            GlAccount.code == payload.code,
            GlAccount.active.is_(True),
        )
    ).first()
    if clash:
        raise HTTPException(status_code=409, detail=f"code {payload.code} is already in use")
    account = GlAccount(
        code=payload.code,
        name=payload.name,
        type=payload.type,
        role=None,          # roles are seed-owned; operator accounts never carry one
        is_system=False,
        company_id=company_id,
    )
    db.add(account)
    _audit(db, company_id, _actor(user), "gl_account_created", {"code": payload.code})
    db.commit()
    return _account_payload(account)


@router.patch("/accounts/{account_id}")
def patch_account(
    account_id: UUID,
    payload: AccountPatchIn,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("accounting.write")),
) -> dict:
    company_id = _tenant_id(user)
    account = db.get(GlAccount, account_id)
    if account is None or account.company_id != company_id:
        raise HTTPException(status_code=404, detail="account not found")

    updates = payload.model_dump(exclude_unset=True)
    if account.is_system and updates.get("active") is False:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{account.code} {account.name} is a system account (role "
                f"{account.role}) — it can be renamed or renumbered but never "
                "deactivated; the posting engine resolves it by role"
            ),
        )

    if updates.get("active") is False:
        # Deactivation cross-check (audit rounds 1+4): a live category
        # mapping — expense OR revenue — must not silently start dangling.
        settings = get_gl_settings(db, company_id)
        mapped = set()
        for map_name in ("expense_category_account_map", "revenue_category_account_map"):
            mapping = (getattr(settings, map_name, None) or {}) if settings else {}
            mapped |= {
                f"{cat} ({map_name.split('_')[0]})"
                for cat, acct_id in mapping.items()
                if acct_id == str(account.id)
            }
        if mapped:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{account.code} is mapped to categories "
                    f"{sorted(mapped)} — remap them before deactivating"
                ),
            )

    # Code-clash check runs on renumber AND on reactivation (audit round 1:
    # deactivate 6100 → create a new 6100 → reactivate the old one made two
    # active accounts share a code).
    becoming_active = updates.get("active") is True and not account.active
    target_code = updates.get("code", account.code)
    if (("code" in updates and updates["code"] != account.code) or becoming_active):
        clash = db.scalars(
            select(GlAccount).where(
                GlAccount.company_id == company_id,
                GlAccount.code == target_code,
                GlAccount.active.is_(True),
                GlAccount.id != account.id,
            )
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail=f"code {target_code} is already in use")

    for field, value in updates.items():
        setattr(account, field, value)
    _audit(
        db, company_id, _actor(user), "gl_account_updated",
        {"account": str(account.id), "fields": sorted(updates.keys())},
    )
    db.commit()
    return _account_payload(account)
