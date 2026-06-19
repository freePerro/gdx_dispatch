"""Pricing-engine admin endpoints — Sprint 1.0.5 Phase 4 + 1.0.6.

CRUD over `pricing_tier_sets`, `margin_tiers`, `pricing_settings`,
`customer_volume_discount_tiers`, `pricing_class_settings`. Powers the
admin tier editor in PricingView.vue.

Surface deliberately narrow:
    GET    /api/pricing-engine/tier-sets                 — list all sets w/ tiers
    PUT    /api/pricing-engine/tier-sets/{id}            — replace tiers wholesale
    GET    /api/pricing-engine/settings                  — singleton get
    PATCH  /api/pricing-engine/settings                  — toggle volume discount
    PUT    /api/pricing-engine/volume-tiers              — replace volume tiers wholesale
    PUT    /api/pricing-engine/class-settings            — per-class volume-discount toggles
    POST   /api/pricing-engine/preview                   — engine-call without persistence

Replace-wholesale semantics for the tier collections (vs piecewise CRUD)
keeps the UI simple — admin edits a table, hits Save, server replaces
all rows for that set in one transaction. No intermediate inconsistent
state visible.

Per CLAUDE.md AI Access triple-layer: every write here is audit-logged
and validated; AI tools can call these endpoints later via the same
typed surface (decision I).

Per CLAUDE.md role gating: requires admin or owner.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.models.pricing_engine import (
    CustomerVolumeDiscountTier,
    MarginTier,
    PricingClassSettings,
    PricingSettings,
    PricingTierSet,
    seed_default_pricing,
)
from gdx_dispatch.core.modules import require_role

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/pricing-engine",
    tags=["pricing-engine"],
    dependencies=[Depends(require_role("admin", "owner"))],
)


# ── Schemas ──────────────────────────────────────────────────────────────────

class TierIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    cost_min: float = Field(ge=0)
    cost_max: float | None = Field(default=None, ge=0)
    margin_pct: float = Field(ge=0, lt=1)


class TierSetOut(BaseModel):
    id: str
    pricing_category: str
    pricing_class: Literal["retail", "contractor", "wholesale"]
    active: bool
    tiers: list[dict]


class TierSetReplaceIn(BaseModel):
    tiers: list[TierIn] = Field(min_length=1, max_length=20)


class SettingsOut(BaseModel):
    id: str
    volume_discount_enabled: bool
    volume_tiers: list[dict]


class SettingsPatchIn(BaseModel):
    volume_discount_enabled: bool | None = None
    # 2026-05-05 — loaded technician cost per hour (wage + burden) used to
    # derive cost_snapshot on labor-matrix lines. 0 = labor not in margin calc.
    loaded_labor_cost_per_hour: float | None = Field(default=None, ge=0, le=999)


class VolumeTierIn(BaseModel):
    volume_min_12mo: float = Field(ge=0)
    volume_max_12mo: float | None = Field(default=None, ge=0)
    discount_pct: float = Field(ge=0, lt=1)


class VolumeTiersReplaceIn(BaseModel):
    tiers: list[VolumeTierIn] = Field(max_length=20)


class ClassSettingIn(BaseModel):
    pricing_class: Literal["retail", "contractor", "wholesale"]
    rolling_volume_discount_enabled: bool


class ClassSettingsReplaceIn(BaseModel):
    classes: list[ClassSettingIn] = Field(min_length=1, max_length=3)


class PreviewLineIn(BaseModel):
    cost: float = Field(ge=0)
    pricing_category: str = Field(min_length=1, max_length=40)
    quantity: float = Field(default=1, gt=0)
    margin_pct_override: float | None = Field(default=None, ge=0, lt=1)


class PreviewIn(BaseModel):
    lines: list[PreviewLineIn] = Field(min_length=1, max_length=200)
    pricing_class: Literal["retail", "contractor", "wholesale"] | None = None
    customer_margin_override: float | None = Field(default=None, ge=0, lt=1)
    # Sprint 1.0.6 — preview can pass a hypothetical rolling-12mo paid
    # volume so admin can dry-run "what would $250k of paid volume look
    # like for this customer class". Defaults to 0 (no discount).
    customer_rolling_volume: float = Field(default=0, ge=0)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialize_tier(t: MarginTier) -> dict:
    return {
        "id": str(t.id),
        "cost_min": float(t.cost_min),
        "cost_max": float(t.cost_max) if t.cost_max is not None else None,
        "margin_pct": float(t.margin_pct),
        "sort_order": t.sort_order,
    }


def _serialize_volume_tier(v: CustomerVolumeDiscountTier) -> dict:
    return {
        "id": str(v.id),
        "volume_min_12mo": float(v.volume_min_12mo),
        "volume_max_12mo": float(v.volume_max_12mo) if v.volume_max_12mo is not None else None,
        "discount_pct": float(v.discount_pct),
        "sort_order": v.sort_order,
    }


def _serialize_class_setting(c: PricingClassSettings) -> dict:
    return {
        "pricing_class": c.pricing_class,
        "rolling_volume_discount_enabled": bool(c.rolling_volume_discount_enabled),
    }


def _serialize_tier_set(ts: PricingTierSet) -> dict:
    return {
        "id": str(ts.id),
        "pricing_category": ts.pricing_category,
        "pricing_class": ts.pricing_class,
        "active": ts.active,
        "tiers": [_serialize_tier(t) for t in sorted(ts.tiers, key=lambda x: x.sort_order)],
    }


def _validate_tier_coverage(tiers: list[TierIn]) -> None:
    """Tier rows must:
    - have lower < upper (or upper=None for top tier)
    - not overlap
    - have ascending sort by cost_min after sort
    Caller guarantees min_length>=1.
    """
    sorted_tiers = sorted(tiers, key=lambda t: t.cost_min)
    for i, t in enumerate(sorted_tiers):
        if t.cost_max is not None and t.cost_max <= t.cost_min:
            raise HTTPException(
                status_code=422,
                detail=f"tier {i}: cost_max ({t.cost_max}) must be > cost_min ({t.cost_min}), or null for open-ended top",
            )
        if i + 1 < len(sorted_tiers):
            nxt = sorted_tiers[i + 1]
            if t.cost_max is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"open-ended tier (cost_max=null) must be the highest; tier {i} is followed by another",
                )
            if nxt.cost_min < t.cost_max:
                raise HTTPException(
                    status_code=422,
                    detail=f"tiers {i}/{i+1} overlap: {t.cost_min}-{t.cost_max} vs {nxt.cost_min}-{nxt.cost_max}",
                )


def _tenant_id(request: Request | None) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    return str(tenant.get("id") or "")


def _user_id(user: dict | None) -> str:
    # Defensive: a `user: dict = Depends(require_role(...))` pattern injects
    # None (require_role returns None — it's a gate, not an injector). Caught
    # at runtime in 1.0.6 audit; injecting via get_current_user fixes it.
    # Keeping the None-tolerance here so a similar copy-paste bug elsewhere
    # logs "system" instead of 500-ing.
    if not user:
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


# ── Tier sets ────────────────────────────────────────────────────────────────


@router.get("/tier-sets", response_model=None)
def list_tier_sets(
    _: dict = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all tier sets (active + inactive) with their tiers, sorted."""
    rows = db.execute(
        select(PricingTierSet).options(selectinload(PricingTierSet.tiers))
        .order_by(PricingTierSet.pricing_category, PricingTierSet.pricing_class)
    ).scalars().all()
    return [_serialize_tier_set(ts) for ts in rows]


@router.put("/tier-sets/{set_id}", response_model=None)
def replace_tiers(
    set_id: UUID,
    payload: TierSetReplaceIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Replace ALL tiers for a tier set in one transaction.

    Validates non-overlap + valid bounds first. On success, deletes old
    tiers and inserts new ones (preserves tier_set row identity so estimates
    referencing it via FK remain valid).
    """
    ts = db.execute(
        select(PricingTierSet).where(PricingTierSet.id == set_id)
        .options(selectinload(PricingTierSet.tiers))
    ).scalar_one_or_none()
    if ts is None:
        raise HTTPException(status_code=404, detail="tier set not found")

    _validate_tier_coverage(payload.tiers)

    # Delete + insert in one txn
    for old in list(ts.tiers):
        db.delete(old)
    db.flush()
    for idx, t in enumerate(sorted(payload.tiers, key=lambda x: x.cost_min)):
        db.add(MarginTier(
            tier_set_id=ts.id,
            cost_min=Decimal(str(t.cost_min)),
            cost_max=Decimal(str(t.cost_max)) if t.cost_max is not None else None,
            margin_pct=Decimal(str(t.margin_pct)),
            sort_order=idx,
        ))
    db.commit()
    db.refresh(ts)

    log_audit_event_sync(
        db=db, tenant_id=_tenant_id(request), user_id=_user_id(user),
        action="pricing_tier_set_replaced",
        entity_type="pricing_tier_set",
        entity_id=str(set_id),
        details={
            "category": ts.pricing_category,
            "class": ts.pricing_class,
            "tier_count": len(payload.tiers),
        },
    )
    db.commit()
    return _serialize_tier_set(ts)


# ── Settings + volume discount ───────────────────────────────────────────────


def _get_or_seed_settings(db: Session) -> PricingSettings:
    """Return the singleton PricingSettings row. Seed if missing.

    Defensive: if a tenant somehow doesn't have a settings row (legacy or
    failed signup), seed it lazily so the admin UI has something to edit
    instead of 500-ing.
    """
    s = db.execute(select(PricingSettings).options(selectinload(PricingSettings.volume_tiers))).scalar_one_or_none()
    if s is None:
        seed_default_pricing(db)
        s = db.execute(select(PricingSettings).options(selectinload(PricingSettings.volume_tiers))).scalar_one()
    return s


@router.get("/settings", response_model=None)
def get_settings(
    _: dict = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> dict:
    s = _get_or_seed_settings(db)
    class_rows = db.execute(select(PricingClassSettings)).scalars().all()
    return {
        "id": str(s.id),
        "volume_discount_enabled": bool(s.volume_discount_enabled),
        "loaded_labor_cost_per_hour": float(s.loaded_labor_cost_per_hour or 0),
        "volume_tiers": [_serialize_volume_tier(v) for v in sorted(s.volume_tiers, key=lambda x: x.sort_order)],
        "class_settings": [_serialize_class_setting(c) for c in class_rows],
    }


@router.patch("/settings", response_model=None)
def patch_settings(
    payload: SettingsPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    s = _get_or_seed_settings(db)
    updates = payload.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)

    log_audit_event_sync(
        db=db, tenant_id=_tenant_id(request), user_id=_user_id(user),
        action="pricing_settings_patched",
        entity_type="pricing_settings",
        entity_id=str(s.id),
        details=updates,
    )
    db.commit()
    return get_settings(_=user, db=db)


@router.put("/volume-tiers", response_model=None)
def replace_volume_tiers(
    payload: VolumeTiersReplaceIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Replace all volume discount tiers wholesale. Empty list clears them."""
    s = _get_or_seed_settings(db)

    # Validate (volume tiers use [volume_min_12mo, volume_max_12mo) semantics)
    if payload.tiers:
        sorted_tiers = sorted(payload.tiers, key=lambda t: t.volume_min_12mo)
        for i, t in enumerate(sorted_tiers):
            if t.volume_max_12mo is not None and t.volume_max_12mo <= t.volume_min_12mo:
                raise HTTPException(status_code=422, detail=f"volume tier {i}: volume_max_12mo must be > volume_min_12mo")
            if i + 1 < len(sorted_tiers):
                nxt = sorted_tiers[i + 1]
                if t.volume_max_12mo is None:
                    raise HTTPException(status_code=422, detail="open-ended volume tier must be highest")
                if nxt.volume_min_12mo < t.volume_max_12mo:
                    raise HTTPException(status_code=422, detail=f"volume tiers {i}/{i+1} overlap")

    for old in list(s.volume_tiers):
        db.delete(old)
    db.flush()
    for idx, t in enumerate(sorted(payload.tiers, key=lambda x: x.volume_min_12mo)):
        db.add(CustomerVolumeDiscountTier(
            settings_id=s.id,
            volume_min_12mo=Decimal(str(t.volume_min_12mo)),
            volume_max_12mo=Decimal(str(t.volume_max_12mo)) if t.volume_max_12mo is not None else None,
            discount_pct=Decimal(str(t.discount_pct)),
            sort_order=idx,
        ))
    db.commit()
    db.refresh(s)

    log_audit_event_sync(
        db=db, tenant_id=_tenant_id(request), user_id=_user_id(user),
        action="volume_discount_tiers_replaced",
        entity_type="pricing_settings",
        entity_id=str(s.id),
        details={"tier_count": len(payload.tiers)},
    )
    db.commit()
    return get_settings(_=user, db=db)


@router.put("/class-settings", response_model=None)
def replace_class_settings(
    payload: ClassSettingsReplaceIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Upsert per-pricing-class volume-discount toggles.

    Sprint 1.0.6 — admin chooses which pricing classes earn the rolling-
    volume discount. Wholesale customers doing real annual volume often
    qualify; one-off retail/contractor work may not. Both gates (master
    + class) must be on for the discount to apply.
    """
    seen: set[str] = set()
    for entry in payload.classes:
        if entry.pricing_class in seen:
            raise HTTPException(
                status_code=422,
                detail=f"duplicate pricing_class entry: {entry.pricing_class}",
            )
        seen.add(entry.pricing_class)

    for entry in payload.classes:
        row = db.execute(
            select(PricingClassSettings).where(
                PricingClassSettings.pricing_class == entry.pricing_class
            )
        ).scalar_one_or_none()
        if row is None:
            row = PricingClassSettings(
                pricing_class=entry.pricing_class,
                rolling_volume_discount_enabled=entry.rolling_volume_discount_enabled,
            )
            db.add(row)
        else:
            row.rolling_volume_discount_enabled = entry.rolling_volume_discount_enabled
    db.commit()

    log_audit_event_sync(
        db=db, tenant_id=_tenant_id(request), user_id=_user_id(user),
        action="pricing_class_settings_replaced",
        entity_type="pricing_class_settings",
        entity_id="all",
        details={
            "classes": {e.pricing_class: e.rolling_volume_discount_enabled for e in payload.classes},
        },
    )
    db.commit()
    return get_settings(_=user, db=db)


# ── Preview (engine call without persistence) ────────────────────────────────


@router.post("/preview", response_model=None)
def preview_pricing(
    payload: PreviewIn,
    _: dict = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> dict:
    """Preview engine output for a hypothetical estimate.

    Powers the admin "try it" widget — admin can set tiers, then preview
    what a sample estimate would look like before saving. No persistence.
    """
    from gdx_dispatch.services.pricing_engine import (
        CustomerView,
        EstimateLineInput,
        PricingConfigError,
        hydrate_settings_from_db,
        price_estimate,
    )

    try:
        settings = hydrate_settings_from_db(db)
        result = price_estimate(
            line_inputs=[
                EstimateLineInput(
                    cost=Decimal(str(ln.cost)),
                    pricing_category=ln.pricing_category,
                    quantity=Decimal(str(ln.quantity)),
                    margin_pct_override=Decimal(str(ln.margin_pct_override)) if ln.margin_pct_override is not None else None,
                ) for ln in payload.lines
            ],
            customer=CustomerView(
                pricing_class=payload.pricing_class,
                margin_override_pct=Decimal(str(payload.customer_margin_override)) if payload.customer_margin_override is not None else None,
                cached_rolling_volume=Decimal(str(payload.customer_rolling_volume)),
            ),
            settings=settings,
        )
    except PricingConfigError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return {
        "subtotal_cost": float(result.subtotal_cost),
        "subtotal_sell_pre_discount": float(result.subtotal_sell_pre_discount),
        "volume_discount_pct": float(result.volume_discount_pct),
        "volume_discount_amount": float(result.volume_discount_amount),
        "subtotal_sell": float(result.subtotal_sell),
        "profit": float(result.profit),
        "blended_margin_pct": float(result.blended_margin_pct),
        "lines": [
            {
                "cost": float(r.price.cost),
                "margin_pct": float(r.price.margin_pct),
                "sell": float(r.price.sell),
                "profit": float(r.price.profit),
                "source": r.price.source,
                "quantity": float(r.inp.quantity),
                "line_sell": float(r.line_sell),
                "line_cost": float(r.line_cost),
                "line_profit": float(r.line_profit),
            } for r in result.lines
        ],
    }
