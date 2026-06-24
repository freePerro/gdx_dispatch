from __future__ import annotations

import logging
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.tenant_ctx import bind_tenant_context, current_tenant_id
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    tags=["pricing"],
    # bind_tenant_context MUST run first so per-tenant state helpers resolve
    # the correct tenant_id when called from inside handlers (Task #32).
    dependencies=[
        Depends(bind_tenant_context),
        Depends(require_module("estimates")),
    ],
)


DEFAULT_PRICING_SETTINGS: dict[str, object] = {
    "margins": {
        "retail": 0.30,
        "contractor": 0.25,
        "wholesale": 0.22,
    },
    "part_cost_tiers": [
        {"min_cost": 0.0, "max_cost": 100.0, "markup_pct": 1.0},
        {"min_cost": 100.0, "max_cost": 500.0, "markup_pct": 0.5},
        {"min_cost": 500.0, "max_cost": None, "markup_pct": 0.3},
    ],
    "volume_discounts": [
        {"annual_spend": 50_000.0, "discount_pct": 0.02},
        {"annual_spend": 250_000.0, "discount_pct": 0.03},
        {"annual_spend": 500_000.0, "discount_pct": 0.04},
        {"annual_spend": 1_000_000.0, "discount_pct": 0.05},
    ],
    "labor_rates": {
        "default": 75.0,
        "tech_overrides": {},
    },
    "adder_rules": {
        "high_lift": 0.0,
        "low_headroom": 0.0,
        "insulation": 0.0,
    },
}

# Task #32 resolved: module-level pricing state is now keyed by tenant_id,
# sourced from gdx_dispatch.core.tenant_ctx.current_tenant_id() which is bound at the
# start of every request via the bind_tenant_context FastAPI dependency.
# Writes from tenant A are scoped to tenant A's slot and invisible to
# tenant B on the same worker.
import logging as _pricing_log_module

log = logging.getLogger(__name__)

_pricing_log = _pricing_log_module.getLogger(__name__)


def _log_tenant_shared_write(slot: str, key: str = "", tenant_id: str = "") -> None:
    """Log tenant-scoped writes for audit trail (Sentry + structured logs).

    Post-#32 this is observability, not a leak warning — every write
    now goes through a per-tenant slot.
    """
    _pricing_log.info(
        "pricing_tenant_write",
        extra={
            "tenant_shared_state": False,
            "slot": slot,
            "key": key or "",
            "tenant_id": tenant_id or current_tenant_id(),
            "task": "task_32_pricing_tenant_isolation_resolved",
        },
    )


# Per-tenant slot dicts. Each key is a tenant_id; each value is the dict
# that was previously the module-level global.
_PRICING_SETTINGS_BY_TENANT: dict[str, dict[str, object]] = {}


def _tenant_settings() -> dict[str, object]:
    """Return (or lazily create) the pricing settings dict for the current tenant."""
    tid = current_tenant_id()
    return _PRICING_SETTINGS_BY_TENANT.setdefault(tid, deepcopy(DEFAULT_PRICING_SETTINGS))


def reset_pricing_state() -> None:
    """Reset all in-memory pricing state. Called by test fixtures."""
    _PRICING_SETTINGS_BY_TENANT.clear()
    _VENDOR_LISTS_BY_TENANT.clear()
    _LOCKED_PRICES_BY_TENANT.clear()
    _SEASONAL_ADJUSTMENTS_BY_TENANT.clear()
    _BUNDLES_BY_TENANT.clear()
    _CUSTOMER_RATES_BY_TENANT.clear()
    if "_APPROVAL_RULES_BY_TENANT" in globals():
        _APPROVAL_RULES_BY_TENANT.clear()


def _money(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _deep_merge_dict(base: dict[str, object], patch: dict[str, object]) -> dict[str, object]:
    out = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = value
    return out


def _get_margin(customer_type: str) -> float:
    customer_key = customer_type.strip().lower()
    margins = _tenant_settings().get("margins", {})
    if not isinstance(margins, dict) or customer_key not in margins:
        raise HTTPException(status_code=422, detail="Unknown customer_type")
    margin = float(margins[customer_key])
    if margin < 0:
        raise HTTPException(status_code=422, detail="Invalid margin value")
    return margin


def _get_volume_discount(annual_spend: float) -> float:
    rules = _tenant_settings().get("volume_discounts", [])
    if not isinstance(rules, list):
        return 0.0
    matched_discount = 0.0
    for rule in sorted(rules, key=lambda row: float(row.get("annual_spend", 0))):  # type: ignore[union-attr]
        threshold = float(rule.get("annual_spend", 0))  # type: ignore[union-attr]
        if annual_spend >= threshold:
            matched_discount = float(rule.get("discount_pct", 0))  # type: ignore[union-attr]
    return matched_discount


def _get_part_tier_markup(cost: float) -> float:
    tiers = _tenant_settings().get("part_cost_tiers", [])
    if not isinstance(tiers, list):
        return 0.0
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        min_cost = float(tier.get("min_cost", 0))
        max_cost = tier.get("max_cost")
        if max_cost is None and cost >= min_cost:
            return float(tier.get("markup_pct", 0.0))
        if max_cost is not None and min_cost <= cost < float(max_cost):
            return float(tier.get("markup_pct", 0.0))
    return 0.0


def _get_labor_rate(tech_id: str | None) -> float:
    labor = _tenant_settings().get("labor_rates", {})
    if not isinstance(labor, dict):
        return 75.0
    default_rate = float(labor.get("default", 75.0))
    overrides = labor.get("tech_overrides", {})
    if tech_id and isinstance(overrides, dict) and tech_id in overrides:
        return float(overrides[tech_id])
    return default_rate


def _adder_total(high_lift: bool, low_headroom: bool, insulation: bool) -> float:
    adders = _tenant_settings().get("adder_rules", {})
    if not isinstance(adders, dict):
        return 0.0
    total = 0.0
    if high_lift:
        total += float(adders.get("high_lift", 0.0))
    if low_headroom:
        total += float(adders.get("low_headroom", 0.0))
    if insulation:
        total += float(adders.get("insulation", 0.0))
    return total


class PricingSettingsPatchIn(BaseModel):
    margins: dict[str, float] | None = None
    part_cost_tiers: list[dict[str, object]] | None = None
    volume_discounts: list[dict[str, object]] | None = None
    labor_rates: dict[str, object] | None = None
    adder_rules: dict[str, float] | None = None


class MarkupItemIn(BaseModel):
    cost: float = Field(ge=0)
    sku: str | None = None
    customer_type: str = "retail"
    annual_spend: float = Field(default=0, ge=0)
    quantity: int = Field(default=1, ge=1)

    @field_validator("customer_type")
    @classmethod
    def _normalize_customer_type(cls, value: str) -> str:
        return value.strip().lower()


class MarkupBatchIn(BaseModel):
    items: list[MarkupItemIn] = Field(default_factory=list, max_length=10000)


@router.get("/api/pricing/settings", response_model=None)
def get_pricing_settings(_: dict = Depends(get_current_user)) -> dict[str, object]:
    return deepcopy(_tenant_settings())


@router.patch("/api/pricing/settings", response_model=None)
def patch_pricing_settings(payload: PricingSettingsPatchIn, _: dict = Depends(require_role("admin", "owner"))) -> dict[str, object]:
    _log_tenant_shared_write("pricing_settings")
    # Mutate the per-tenant slot in place
    current = _tenant_settings()
    merged = _deep_merge_dict(current, payload.model_dump(exclude_unset=True))
    _PRICING_SETTINGS_BY_TENANT[current_tenant_id()] = merged
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
                action="patch_pricing_settings",
                entity_type="pricing_setting",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_pricing_settings_audit_failed')
    return deepcopy(_tenant_settings())


@router.get("/api/pricing/calculate", response_model=None)
def calculate_sell_price(
    cost: Annotated[float, Query(ge=0)],
    customer_type: Annotated[str, Query(min_length=1)] = "retail",
    annual_spend: Annotated[float, Query(ge=0)] = 0,
    labor_hours: Annotated[float, Query(ge=0)] = 0,
    tech_id: Annotated[str | None, Query()] = None,
    high_lift: bool = False,
    low_headroom: bool = False,
    insulation: bool = False,
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    margin = _get_margin(customer_type)
    base_sell = cost * (1 + margin)

    labor_rate = _get_labor_rate(tech_id)
    labor_total = labor_rate * labor_hours
    adder_total = _adder_total(high_lift=high_lift, low_headroom=low_headroom, insulation=insulation)

    pre_discount = base_sell + labor_total + adder_total
    discount_pct = _get_volume_discount(annual_spend)
    discounted = pre_discount * (1 - discount_pct)

    return {
        "cost": _money(cost),
        "customer_type": customer_type.strip().lower(),
        "margin_pct": margin,
        "labor_rate": _money(labor_rate),
        "labor_hours": labor_hours,
        "labor_total": _money(labor_total),
        "adder_total": _money(adder_total),
        "volume_discount_pct": discount_pct,
        "sell_price": _money(discounted),
    }


@router.post("/api/pricing/markup", response_model=None)
def calculate_markup(payload: MarkupBatchIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    grand_total = 0.0
    for item in payload.items:
        markup_pct = _get_part_tier_markup(item.cost)
        sell_price = _money(item.cost * (1 + markup_pct))
        line_total = _money(sell_price * item.quantity)
        rows.append(
            {
                "sku": item.sku,
                "cost": _money(item.cost),
                "markup_pct": markup_pct,
                "sell_price": sell_price,
                "quantity": item.quantity,
                "line_total": line_total,
                "customer_type": item.customer_type,
                "annual_spend": item.annual_spend,
            }
        )
        grand_total += line_total
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
                action="calculate_markup",
                entity_type="calculate_markup",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('calculate_markup_audit_failed')
    return {"items": rows, "grand_total": _money(grand_total)}


# ---------------------------------------------------------------------------
# Vendor Price Lists (#137)
# ---------------------------------------------------------------------------

_VENDOR_LISTS_BY_TENANT: dict[str, dict[str, dict[str, object]]] = {}

def _tenant_vendor_lists() -> dict[str, dict[str, object]]:
    return _VENDOR_LISTS_BY_TENANT.setdefault(current_tenant_id(), {})


class VendorPriceItem(BaseModel):
    vendor_name: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    description: str = ""
    cost: float = Field(ge=0)


class VendorPriceBatchIn(BaseModel):
    items: list[VendorPriceItem] = Field(default_factory=list, max_length=10000)


@router.post("/api/pricing/vendor-lists")
def import_vendor_prices(payload: VendorPriceBatchIn, _: dict = Depends(require_role("admin", "owner"))) -> dict[str, object]:
    imported = 0
    for item in payload.items:
        key = f"{item.vendor_name}:{item.sku}"
        _log_tenant_shared_write("vendor_lists", key=key)
        _tenant_vendor_lists()[key] = item.model_dump()
        imported += 1
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
                action="import_vendor_prices",
                entity_type="vendor_price",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('import_vendor_prices_audit_failed')
    return {"imported": imported, "total": len(_tenant_vendor_lists())}


@router.get("/api/pricing/vendor-lists")
def list_vendor_prices(
    vendor: str | None = None,
    _: dict = Depends(get_current_user),
) -> list[dict[str, object]]:
    # Data lives in-memory (_tenant_vendor_lists() dict), no DB query — Redis cache unnecessary
    items = list(_tenant_vendor_lists().values())
    if vendor:
        items = [i for i in items if i.get("vendor_name", "").lower() == vendor.lower()]
    return items


# ---------------------------------------------------------------------------
# Price Comparison (#144)
# ---------------------------------------------------------------------------

@router.get("/api/pricing/comparison")
def price_comparison(
    customer_type: str = "retail",
    _: dict = Depends(get_current_user),
) -> list[dict[str, object]]:
    """Show cost vs sell price vs margin for all vendor-listed items."""
    margin = _get_margin(customer_type)
    results = []
    for _key, item in _tenant_vendor_lists().items():
        cost = float(item.get("cost", 0))
        sell = _money(cost * (1 + margin))
        profit = _money(sell - cost)
        margin_pct = _money(profit / sell * 100) if sell > 0 else 0
        results.append({
            "vendor": item.get("vendor_name"),
            "sku": item.get("sku"),
            "description": item.get("description"),
            "cost": _money(cost),
            "sell_price": sell,
            "profit": profit,
            "margin_pct": margin_pct,
            "customer_type": customer_type,
        })
    return results


# ---------------------------------------------------------------------------
# Price Book Versioning (#143)
# ---------------------------------------------------------------------------

_LOCKED_PRICES_BY_TENANT: dict[str, dict[str, dict[str, object]]] = {}

def _tenant_locked_prices() -> dict[str, dict[str, object]]:
    return _LOCKED_PRICES_BY_TENANT.setdefault(current_tenant_id(), {})


class LockPricesIn(BaseModel):
    estimate_id: str = Field(min_length=1, max_length=64)
    line_items: list[dict[str, object]] = Field(default_factory=list, max_length=1000)


@router.post("/api/pricing/lock-prices")
def lock_estimate_prices(payload: LockPricesIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    """Snapshot current prices when an estimate is accepted."""
    snapshot = {
        "estimate_id": payload.estimate_id,
        "line_items": payload.line_items,
        "pricing_settings": deepcopy(_tenant_settings()),
    }
    _log_tenant_shared_write("locked_prices", key=str(payload.estimate_id))
    _tenant_locked_prices()[payload.estimate_id] = snapshot
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
                action="lock_estimate_prices",
                entity_type="lock_estimate_price",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('lock_estimate_prices_audit_failed')
    return {"locked": True, "estimate_id": payload.estimate_id}


@router.get("/api/pricing/locked/{estimate_id}")
def get_locked_prices(estimate_id: str, _: dict = Depends(get_current_user)) -> dict[str, object]:
    if estimate_id not in _tenant_locked_prices():
        raise HTTPException(status_code=404, detail="No locked prices for this estimate")
    return _tenant_locked_prices()[estimate_id]


# ---------------------------------------------------------------------------
# Seasonal Pricing (#146)
# ---------------------------------------------------------------------------

_SEASONAL_ADJUSTMENTS_BY_TENANT: dict[str, dict[str, dict[str, float]]] = {}

def _tenant_seasonal_adjustments() -> dict[str, dict[str, float]]:
    return _SEASONAL_ADJUSTMENTS_BY_TENANT.setdefault(current_tenant_id(), {})


class SeasonalAdjustment(BaseModel):
    category: str = Field(min_length=1)
    season: str = Field(pattern="^(summer|winter|spring|fall)$")
    adjustment_pct: float = Field(ge=-0.5, le=0.5)


@router.get("/api/pricing/seasonal")
def get_seasonal_pricing(_: dict = Depends(get_current_user)) -> list[dict[str, object]]:
    return [{"category": k.split(":")[0], "season": k.split(":")[1], "adjustment_pct": v}
            for k, v in _tenant_seasonal_adjustments().items()]


@router.patch("/api/pricing/seasonal")
def set_seasonal_pricing(payload: SeasonalAdjustment, _: dict = Depends(require_role("admin", "owner"))) -> dict[str, object]:
    key = f"{payload.category}:{payload.season}"
    _log_tenant_shared_write("seasonal_adjustments", key=key)
    _tenant_seasonal_adjustments()[key] = payload.adjustment_pct
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
                action="set_seasonal_pricing",
                entity_type="seasonal_pricing",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('set_seasonal_pricing_audit_failed')
    return {"category": payload.category, "season": payload.season, "adjustment_pct": payload.adjustment_pct}


# ---------------------------------------------------------------------------
# Bundle Pricing (#147)
# ---------------------------------------------------------------------------

_BUNDLES_BY_TENANT: dict[str, dict[str, dict[str, object]]] = {}

def _tenant_bundles() -> dict[str, dict[str, object]]:
    return _BUNDLES_BY_TENANT.setdefault(current_tenant_id(), {})


class BundleItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=100000)
    unit_price: float = Field(ge=0, le=1_000_000)


class BundleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    items: list[BundleItemIn] = Field(max_length=500)
    bundle_discount_pct: float = Field(default=0, ge=0, le=50)


@router.post("/api/pricing/bundles")
def create_bundle(payload: BundleIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    _log_tenant_shared_write("bundles", key=payload.name)
    _tenant_bundles()[payload.name] = payload.model_dump()
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
                action="create_bundle",
                entity_type="bundle",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_bundle_audit_failed')
    return {"created": True, "name": payload.name, "id": payload.name}


@router.get("/api/pricing/bundles")
def list_bundles(_: dict = Depends(get_current_user)) -> list[dict[str, object]]:
    return list(_tenant_bundles().values())


@router.post("/api/pricing/bundles/{bundle_id}/calculate")
def calculate_bundle_by_id(bundle_id: str, _: dict = Depends(get_current_user)) -> dict[str, object]:
    bundle = _tenant_bundles().get(bundle_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    name = bundle.get("name", bundle_id)
    subtotal = sum(i["unit_price"] * i["quantity"] for i in bundle["items"])
    discount = float(bundle.get("bundle_discount_pct", 0)) / 100
    total = _money(subtotal * (1 - discount))
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
                action="calculate_bundle_by_id",
                entity_type="calculate_bundle",
                entity_id=str(bundle_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('calculate_bundle_by_id_audit_failed')
    return {"name": name, "subtotal": _money(subtotal), "discount_pct": bundle["bundle_discount_pct"], "total": total}


@router.post("/api/pricing/bundles/calculate")
def calculate_bundle(name: str = Query(min_length=1), _: dict = Depends(get_current_user)) -> dict[str, object]:
    bundle = _tenant_bundles().get(name)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    subtotal = sum(i["unit_price"] * i["quantity"] for i in bundle["items"])
    discount = float(bundle.get("bundle_discount_pct", 0)) / 100
    total = _money(subtotal * (1 - discount))
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
                action="calculate_bundle",
                entity_type="calculate_bundle",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('calculate_bundle_audit_failed')
    return {"name": name, "subtotal": _money(subtotal), "discount_pct": bundle["bundle_discount_pct"], "total": total}


# ---------------------------------------------------------------------------
# Customer-Specific Pricing (#148)
# ---------------------------------------------------------------------------

_CUSTOMER_RATES_BY_TENANT: dict[str, dict[str, dict[str, object]]] = {}

def _tenant_customer_rates() -> dict[str, dict[str, object]]:
    return _CUSTOMER_RATES_BY_TENANT.setdefault(current_tenant_id(), {})


class CustomerRateIn(BaseModel):
    customer_id: str = Field(min_length=1)
    discount_pct: float = Field(default=0, ge=0, le=50)
    custom_labor_rate: float | None = None


@router.post("/api/pricing/customer-rates")
def set_customer_rate(payload: CustomerRateIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    _log_tenant_shared_write("customer_rates", key=str(payload.customer_id))
    _tenant_customer_rates()[payload.customer_id] = payload.model_dump()
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
                action="set_customer_rate",
                entity_type="customer_rate",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('set_customer_rate_audit_failed')
    return {"customer_id": payload.customer_id, "discount_pct": payload.discount_pct}


@router.get("/api/pricing/customer-rates")
def list_customer_rates(_: dict = Depends(get_current_user)) -> list[dict[str, object]]:
    return list(_tenant_customer_rates().values())


@router.get("/api/pricing/customer-rates/{customer_id}")
def get_customer_rate(customer_id: str, _: dict = Depends(get_current_user)) -> dict[str, object]:
    rate = _tenant_customer_rates().get(customer_id)
    if not rate:
        raise HTTPException(status_code=404, detail="No custom rate for this customer")
    return rate


# ---------------------------------------------------------------------------
# Price Approval Workflow (#149)
# ---------------------------------------------------------------------------

_APPROVAL_RULES_BY_TENANT: dict[str, list[dict[str, object]]] = {}

def _tenant_approval_rules() -> list[dict[str, object]]:
    return _APPROVAL_RULES_BY_TENANT.setdefault(current_tenant_id(), [])


class ApprovalRuleIn(BaseModel):
    threshold_amount: float = Field(gt=0, le=10_000_000)
    required_role: str = Field(default="admin", max_length=50)


class ApprovalCheckIn(BaseModel):
    quote_amount: float = Field(ge=0, le=10_000_000)
    user_role: str = Field(max_length=50)


@router.post("/api/pricing/approval-rules")
def set_approval_rule(payload: ApprovalRuleIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    _tenant_approval_rules().append(payload.model_dump())
    _tenant_approval_rules().sort(key=lambda r: float(r["threshold_amount"]))
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
                action="set_approval_rule",
                entity_type="approval_rule",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('set_approval_rule_audit_failed')
    return {"rules": _tenant_approval_rules()}


@router.get("/api/pricing/approval-rules")
def get_approval_rules(_: dict = Depends(get_current_user)) -> list[dict[str, object]]:
    return _tenant_approval_rules()


@router.post("/api/pricing/check-approval")
def check_approval(payload: ApprovalCheckIn, _: dict = Depends(get_current_user)) -> dict[str, object]:
    """Check if a quote amount requires manager approval."""
    for rule in reversed(_tenant_approval_rules()):
        if payload.quote_amount >= float(rule["threshold_amount"]):
            required_role = str(rule["required_role"])
            return {
                "requires_approval": payload.user_role != required_role,
                "threshold": float(rule["threshold_amount"]),
                "required_role": required_role,
                "user_role": payload.user_role,
            }
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
                action="check_approval",
                entity_type="check_approval",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('check_approval_audit_failed')
    return {"requires_approval": False, "threshold": None, "required_role": None, "user_role": payload.user_role}
