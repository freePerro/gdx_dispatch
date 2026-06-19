"""Pure pricing engine — cost → sell math.

Sprint 1.0.5. Single source of truth replacing the in-memory state in
`gdx_dispatch/routers/pricing.py` and the hardcoded margin tiers in
`gdx_dispatch/routers/door_catalog.py`.

Math
----
margin_pct is a true MARGIN (profit / sell), Decimal in [0, 1):
    sell   = cost / (1 - margin_pct)
    profit = sell - cost
    margin_pct == profit / sell  (definitional)

Resolution order (per line, highest precedence first)
-----------------------------------------------------
1. line.margin_pct_override          → source = "line_override"
2. customer.margin_override_pct      → source = "customer_override"
3. wholesale tier  (if class==wholesale)  → source = "wholesale_tier"
4. retail/contractor tier            → source = "tier"

Tier ranges are [cost_min, cost_max) — lower inclusive, upper exclusive.
cost_max=None means open-ended top tier. Exactly ONE tier must match a
given cost; overlapping or gap-having tier sets are a configuration bug
and the engine fails loud (PricingConfigError).

Volume discount (Sprint 1.0.6 — customer rolling-volume basis)
---------------
Two-level enable: the discount applies only when BOTH
  - `settings.volume_discount_enabled` (master), AND
  - `customer.class_volume_discount_enabled` (per-pricing-class toggle)
are True. We then look up the [volume_min_12mo, volume_max_12mo) tier
matching the customer's `cached_rolling_volume_paid_12mo` and apply
discount_pct as a straight reduction on the sell side. Customer pays
less; profit drops by the same dollar amount.

Cliff (Salesforce "Range" mode, not "Slab"): once a customer's rolling
volume crosses a threshold, the full tier discount applies to the entire
estimate sell subtotal.

Replaces 1.0.5's per-estimate-subtotal volume discount placeholder. Real
customers think in account spend over a year, not single-job size.

Pure
----
No DB calls, no I/O. Caller hydrates `PricingSettings` from the DB and
passes it in. This makes the engine trivially testable AND callable from
any context (background task, AI tool, batch script).

Money is Decimal. Quantization is the caller's job — engine returns
unrounded Decimals so successive operations don't accumulate rounding
error.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Literal, Optional

# Type aliases for readability
PricingClass = Literal["retail", "contractor", "wholesale"]
PricingSource = Literal["line_override", "customer_override", "wholesale_tier", "tier"]

VALID_CLASSES: frozenset[str] = frozenset({"retail", "contractor", "wholesale"})


class PricingConfigError(Exception):
    """Raised when the pricing settings can't resolve a sane sell price.

    Examples: no tier matches a given cost; tier set has an invalid
    margin (>= 1.0); overlapping tier ranges; missing tier set entirely.

    Per CLAUDE.md zero-tolerance — never silently fall back to 0% margin
    or some "default" — that quietly miscosts every estimate.
    """


# ---------------------------------------------------------------------------
# Plain dataclasses for inputs/outputs — keep the engine free of ORM coupling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierRow:
    cost_min: Decimal
    cost_max: Optional[Decimal]  # None = open-ended top
    margin_pct: Decimal


@dataclass(frozen=True)
class VolumeTierRow:
    volume_min_12mo: Decimal
    volume_max_12mo: Optional[Decimal]
    discount_pct: Decimal


@dataclass(frozen=True)
class PricingSettingsView:
    """Hydrated, plain-data view the engine consumes.

    Caller builds this from the ORM (`PricingTierSet`, `MarginTier`,
    `PricingSettings`, `CustomerVolumeDiscountTier`,
    `PricingClassSettings`).
    """

    # tier_sets[(category, class)] = [tier rows sorted by cost_min ASC]
    tier_sets: dict[tuple[str, PricingClass], list[TierRow]]
    volume_discount_enabled: bool  # master switch
    volume_tiers: list[VolumeTierRow]
    # class_volume_enabled[<pricing_class>] = bool — per-class second gate
    class_volume_enabled: dict[PricingClass, bool]


@dataclass(frozen=True)
class CustomerView:
    """Engine input. Pull only what the engine needs from the ORM Customer."""

    pricing_class: Optional[PricingClass]  # None → engine uses 'retail'
    margin_override_pct: Optional[Decimal]
    # Sprint 1.0.6 — denormalized 365d paid-volume cache, used by the
    # rolling-volume discount lookup. Default 0 means no discount applies.
    cached_rolling_volume: Decimal = Decimal("0")


@dataclass(frozen=True)
class LinePrice:
    cost: Decimal
    margin_pct: Decimal
    sell: Decimal
    profit: Decimal
    source: PricingSource


@dataclass(frozen=True)
class EstimateLineInput:
    cost: Decimal
    pricing_category: str
    quantity: Decimal = Decimal("1")
    margin_pct_override: Optional[Decimal] = None


@dataclass(frozen=True)
class EstimateLineResult:
    inp: EstimateLineInput
    price: LinePrice
    line_sell: Decimal  # price.sell × quantity, pre-volume-discount
    line_cost: Decimal  # cost × quantity
    line_profit: Decimal  # line_sell − line_cost (pre-discount)


@dataclass(frozen=True)
class EstimateTotals:
    lines: list[EstimateLineResult]
    subtotal_cost: Decimal
    subtotal_sell_pre_discount: Decimal
    volume_discount_pct: Decimal  # 0 if not applicable
    volume_discount_amount: Decimal  # absolute $ off the sell side
    subtotal_sell: Decimal  # post-discount
    profit: Decimal  # subtotal_sell − subtotal_cost
    blended_margin_pct: Decimal  # profit / subtotal_sell, 0 if subtotal_sell==0


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _validate_margin(margin_pct: Decimal, *, where: str) -> None:
    if margin_pct < _ZERO:
        raise PricingConfigError(f"{where}: margin_pct {margin_pct} is negative")
    if margin_pct >= _ONE:
        raise PricingConfigError(
            f"{where}: margin_pct {margin_pct} >= 1.0 — implies infinite sell price"
        )


def sell_from_cost(cost: Decimal, margin_pct: Decimal) -> Decimal:
    """sell = cost / (1 - margin_pct).

    Both arguments must be Decimal. Caller responsible for ensuring
    cost >= 0 and validating margin via _validate_margin().
    """
    _validate_margin(margin_pct, where="sell_from_cost")
    if cost < _ZERO:
        raise PricingConfigError(f"sell_from_cost: cost {cost} is negative")
    return cost / (_ONE - margin_pct)


# ---------------------------------------------------------------------------
# Tier lookup
# ---------------------------------------------------------------------------


def _resolve_class(customer: CustomerView) -> PricingClass:
    """Default a customer with no class set → 'retail'."""
    cls = customer.pricing_class
    if cls is None:
        return "retail"
    if cls not in VALID_CLASSES:
        raise PricingConfigError(
            f"customer.pricing_class={cls!r} is not one of {sorted(VALID_CLASSES)}"
        )
    return cls  # type: ignore[return-value]


def _find_tier(
    tier_set: list[TierRow],
    cost: Decimal,
    *,
    where: str,
) -> TierRow:
    """Return the unique tier whose [cost_min, cost_max) contains cost.

    Fail loud if zero or more than one matches — that's a config error,
    not a runtime fallback case.
    """
    matches = [
        t for t in tier_set
        if cost >= t.cost_min and (t.cost_max is None or cost < t.cost_max)
    ]
    if not matches:
        raise PricingConfigError(
            f"{where}: no tier matches cost={cost}. Tier set covers "
            f"{[(str(t.cost_min), str(t.cost_max)) for t in tier_set]}"
        )
    if len(matches) > 1:
        raise PricingConfigError(
            f"{where}: cost={cost} matches {len(matches)} overlapping tiers — "
            f"tier set must have non-overlapping ranges"
        )
    return matches[0]


def _lookup_tier_set(
    settings: PricingSettingsView,
    pricing_category: str,
    pricing_class: PricingClass,
) -> list[TierRow]:
    key = (pricing_category, pricing_class)
    tier_set = settings.tier_sets.get(key)
    if tier_set is None:
        raise PricingConfigError(
            f"no tier set configured for category={pricing_category!r} "
            f"class={pricing_class!r}"
        )
    if not tier_set:
        raise PricingConfigError(
            f"tier set for category={pricing_category!r} class={pricing_class!r} "
            f"is empty"
        )
    return tier_set


# ---------------------------------------------------------------------------
# Per-line resolution
# ---------------------------------------------------------------------------


def price_line(
    cost: Decimal,
    pricing_category: str,
    customer: CustomerView,
    settings: PricingSettingsView,
    *,
    line_margin_override: Optional[Decimal] = None,
) -> LinePrice:
    """Resolve sell price + profit + which input won."""
    if not isinstance(cost, Decimal):
        raise PricingConfigError(f"cost must be Decimal, got {type(cost).__name__}")

    # Doug 2026-05-07 / EST-000030 retro: labor lines never tier-markup.
    # flat_price on the matrix row IS the customer-facing sell. Routing
    # labor through this engine produced the $91k cascade. Catch any
    # regression that re-introduces the path.
    if (pricing_category or "").lower() == "labor":
        raise PricingConfigError(
            "labor lines must not flow through the tier engine — "
            "use the labor matrix (LaborPriceItem) and "
            "estimates._labor_line_pricing instead"
        )

    # 1. line override beats everything
    if line_margin_override is not None:
        _validate_margin(line_margin_override, where="line_margin_override")
        sell = sell_from_cost(cost, line_margin_override)
        return LinePrice(cost, line_margin_override, sell, sell - cost, "line_override")

    # 2. customer-level flat override
    if customer.margin_override_pct is not None:
        _validate_margin(
            customer.margin_override_pct, where="customer.margin_override_pct"
        )
        sell = sell_from_cost(cost, customer.margin_override_pct)
        return LinePrice(
            cost, customer.margin_override_pct, sell, sell - cost, "customer_override"
        )

    # 3 + 4. tier lookup
    cls = _resolve_class(customer)
    tier_set = _lookup_tier_set(settings, pricing_category, cls)
    tier = _find_tier(
        tier_set, cost, where=f"category={pricing_category} class={cls}"
    )
    _validate_margin(tier.margin_pct, where=f"tier {tier}")
    sell = sell_from_cost(cost, tier.margin_pct)
    source: PricingSource = "wholesale_tier" if cls == "wholesale" else "tier"
    return LinePrice(cost, tier.margin_pct, sell, sell - cost, source)


# ---------------------------------------------------------------------------
# Estimate totals + volume discount
# ---------------------------------------------------------------------------


def _find_volume_discount(
    settings: PricingSettingsView, customer: CustomerView
) -> Decimal:
    """Return the matching discount_pct or 0 if none / disabled.

    Two-level enable:
      1. settings.volume_discount_enabled (master)
      2. settings.class_volume_enabled[<resolved class>] (per-class)

    Lookup keys on customer.cached_rolling_volume (trailing-365-day paid
    volume), NOT on the estimate sell subtotal.
    """
    if not settings.volume_discount_enabled:
        return _ZERO
    if not settings.volume_tiers:
        return _ZERO
    cls = _resolve_class(customer)
    # Per-class gate. Missing key → treat as disabled (defensive: engine
    # never silently extends a discount to a class admin didn't opt in).
    if not settings.class_volume_enabled.get(cls, False):
        return _ZERO
    volume = customer.cached_rolling_volume
    matches = [
        t for t in settings.volume_tiers
        if volume >= t.volume_min_12mo
        and (t.volume_max_12mo is None or volume < t.volume_max_12mo)
    ]
    if not matches:
        return _ZERO  # below the lowest tier — no discount, NOT an error
    if len(matches) > 1:
        raise PricingConfigError(
            f"customer rolling volume={volume} matches {len(matches)} "
            f"overlapping volume tiers"
        )
    pct = matches[0].discount_pct
    if pct < _ZERO or pct >= _ONE:
        raise PricingConfigError(
            f"volume tier discount_pct {pct} out of [0, 1) range"
        )
    return pct


def price_estimate(
    line_inputs: Iterable[EstimateLineInput],
    customer: CustomerView,
    settings: PricingSettingsView,
) -> EstimateTotals:
    """Price a whole estimate. Per-line first, then volume discount on subtotal."""
    results: list[EstimateLineResult] = []
    subtotal_cost = _ZERO
    subtotal_sell = _ZERO

    for inp in line_inputs:
        if inp.quantity <= _ZERO:
            raise PricingConfigError(
                f"line quantity must be > 0, got {inp.quantity}"
            )
        price = price_line(
            inp.cost,
            inp.pricing_category,
            customer,
            settings,
            line_margin_override=inp.margin_pct_override,
        )
        line_sell = price.sell * inp.quantity
        line_cost = price.cost * inp.quantity
        results.append(
            EstimateLineResult(
                inp=inp,
                price=price,
                line_sell=line_sell,
                line_cost=line_cost,
                line_profit=line_sell - line_cost,
            )
        )
        subtotal_cost += line_cost
        subtotal_sell += line_sell

    discount_pct = _find_volume_discount(settings, customer)
    discount_amount = subtotal_sell * discount_pct
    subtotal_sell_post = subtotal_sell - discount_amount
    profit = subtotal_sell_post - subtotal_cost
    blended = (profit / subtotal_sell_post) if subtotal_sell_post > _ZERO else _ZERO

    return EstimateTotals(
        lines=results,
        subtotal_cost=subtotal_cost,
        subtotal_sell_pre_discount=subtotal_sell,
        volume_discount_pct=discount_pct,
        volume_discount_amount=discount_amount,
        subtotal_sell=subtotal_sell_post,
        profit=profit,
        blended_margin_pct=blended,
    )


# ---------------------------------------------------------------------------
# ORM hydration (small convenience for callers — keeps engine pure)
# ---------------------------------------------------------------------------


def hydrate_settings_from_db(session) -> PricingSettingsView:
    """Build a PricingSettingsView from the ORM. Pure read, no side effects.

    Caller passes a tenant-scoped session. Returns a frozen dataclass the
    engine can consume.
    """
    # Lazy import to avoid circular dep at module load
    from gdx_dispatch.models.pricing_engine import (
        CustomerVolumeDiscountTier,
        MarginTier,
        PricingClassSettings,
        PricingSettings,
        PricingTierSet,
    )

    sets: dict[tuple[str, PricingClass], list[TierRow]] = {}
    for ts in session.query(PricingTierSet).filter_by(active=True).all():
        rows = (
            session.query(MarginTier)
            .filter_by(tier_set_id=ts.id)
            .order_by(MarginTier.sort_order)
            .all()
        )
        sets[(ts.pricing_category, ts.pricing_class)] = [
            TierRow(
                cost_min=Decimal(r.cost_min),
                cost_max=Decimal(r.cost_max) if r.cost_max is not None else None,
                margin_pct=Decimal(r.margin_pct),
            )
            for r in rows
        ]

    settings_row = session.query(PricingSettings).first()
    if settings_row is None:
        # Fail loud — every tenant should be seeded at signup/pave
        raise PricingConfigError(
            "no PricingSettings row in this tenant DB — seed_default_pricing() "
            "was not called at signup/pave"
        )

    vtiers = (
        session.query(CustomerVolumeDiscountTier)
        .filter_by(settings_id=settings_row.id)
        .order_by(CustomerVolumeDiscountTier.sort_order)
        .all()
    )

    # Per-class enable flags. Missing rows default to disabled — the
    # engine treats unseed/unknown classes as opt-out, never opt-in.
    class_enabled: dict[PricingClass, bool] = {"retail": False, "contractor": False, "wholesale": False}
    for row in session.query(PricingClassSettings).all():
        if row.pricing_class in class_enabled:
            class_enabled[row.pricing_class] = bool(row.rolling_volume_discount_enabled)

    return PricingSettingsView(
        tier_sets=sets,
        volume_discount_enabled=bool(settings_row.volume_discount_enabled),
        volume_tiers=[
            VolumeTierRow(
                volume_min_12mo=Decimal(v.volume_min_12mo),
                volume_max_12mo=Decimal(v.volume_max_12mo) if v.volume_max_12mo is not None else None,
                discount_pct=Decimal(v.discount_pct),
            )
            for v in vtiers
        ],
        class_volume_enabled=class_enabled,
    )
