"""Pricing engine unit tests — Sprint 1.0.5.

Tests the pure engine in isolation. No DB. Every assertion's expected
value is hand-computed from the spec so a regression is caught loud.

Math being verified:
    sell   = cost / (1 - margin_pct)
    profit = sell - cost
    margin_pct == profit / sell  (definitional check, exercised in test_definitional_identity)
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from gdx_dispatch.services.pricing_engine import (
    CustomerView,
    EstimateLineInput,
    PricingConfigError,
    PricingSettingsView,
    TierRow,
    VolumeTierRow,
    _find_tier,
    _resolve_class,
    _validate_margin,
    price_estimate,
    price_line,
    sell_from_cost,
)


# ---------------------------------------------------------------------------
# Test fixtures (plain data — no DB)
# ---------------------------------------------------------------------------

def _stub_settings(
    *,
    volume_enabled: bool = False,
    volume_tiers=None,
    class_enabled: dict | None = None,
) -> PricingSettingsView:
    """Build a settings object covering doors+parts × all 3 classes.

    Tier numbers are deliberately chosen so each test's expected output
    can be verified by hand.

    Sprint 1.0.6 — `class_enabled` defaults to all-classes-on so existing
    tests that flip `volume_enabled` continue to see a discount apply.
    """
    retail_doors = [
        TierRow(Decimal("0"), Decimal("100"), Decimal("0.5")),    # 50%
        TierRow(Decimal("100"), Decimal("500"), Decimal("0.4")),  # 40%
        TierRow(Decimal("500"), None, Decimal("0.25")),           # 25%, open
    ]
    retail_parts = [
        TierRow(Decimal("0"), Decimal("50"), Decimal("0.6")),     # 60%
        TierRow(Decimal("50"), None, Decimal("0.3")),
    ]
    contractor_doors = [
        TierRow(Decimal("0"), None, Decimal("0.2")),  # flat 20% open
    ]
    contractor_parts = [
        TierRow(Decimal("0"), None, Decimal("0.25")),
    ]
    wholesale_doors = [
        TierRow(Decimal("0"), None, Decimal("0.15")),
    ]
    wholesale_parts = [
        TierRow(Decimal("0"), None, Decimal("0.2")),
    ]
    if class_enabled is None:
        class_enabled = {"retail": True, "contractor": True, "wholesale": True}
    return PricingSettingsView(
        tier_sets={
            ("doors", "retail"): retail_doors,
            ("doors", "contractor"): contractor_doors,
            ("doors", "wholesale"): wholesale_doors,
            ("parts", "retail"): retail_parts,
            ("parts", "contractor"): contractor_parts,
            ("parts", "wholesale"): wholesale_parts,
        },
        volume_discount_enabled=volume_enabled,
        volume_tiers=volume_tiers or [],
        class_volume_enabled=class_enabled,
    )


def _customer(cls=None, override=None, rolling_volume=Decimal("0")) -> CustomerView:
    return CustomerView(
        pricing_class=cls,
        margin_override_pct=override,
        cached_rolling_volume=rolling_volume,
    )


# ---------------------------------------------------------------------------
# Math primitives
# ---------------------------------------------------------------------------

def test_sell_from_cost_simple():
    # 100 / (1 - 0.5) = 200
    assert sell_from_cost(Decimal("100"), Decimal("0.5")) == Decimal("200")


def test_sell_from_cost_quarter_margin():
    # 100 / (1 - 0.25) = 100 / 0.75 = 133.333...
    sell = sell_from_cost(Decimal("100"), Decimal("0.25"))
    # 400/3 exactly
    assert sell == Decimal("100") / Decimal("0.75")


def test_sell_from_cost_zero_margin_passthrough():
    # 0% margin: sell = cost (we sell at cost — losing money but valid)
    assert sell_from_cost(Decimal("250"), Decimal("0")) == Decimal("250")


def test_validate_margin_rejects_one():
    with pytest.raises(PricingConfigError, match="infinite sell"):
        _validate_margin(Decimal("1.0"), where="t")


def test_validate_margin_rejects_above_one():
    with pytest.raises(PricingConfigError, match="infinite sell"):
        _validate_margin(Decimal("1.5"), where="t")


def test_validate_margin_rejects_negative():
    with pytest.raises(PricingConfigError, match="negative"):
        _validate_margin(Decimal("-0.1"), where="t")


def test_sell_from_cost_rejects_invalid_margin():
    with pytest.raises(PricingConfigError):
        sell_from_cost(Decimal("100"), Decimal("1.0"))


def test_sell_from_cost_rejects_negative_cost():
    with pytest.raises(PricingConfigError, match="negative"):
        sell_from_cost(Decimal("-5"), Decimal("0.3"))


def test_definitional_identity_exact_cases():
    """profit / sell == margin_pct, using exactly-divisible Decimal pairs.

    Note: in real math the identity holds for ANY cost+margin, but Decimal
    has finite precision (28 sig figs default). For arbitrary inputs we'd
    need an epsilon comparison; here we use cost+margin pairs whose results
    are exactly representable so we can assert equality and catch real bugs.
    """
    # (cost, margin, expected_sell, expected_profit) — all exact in Decimal
    cases = [
        (Decimal("100"), Decimal("0.5"),  Decimal("200"), Decimal("100")),
        (Decimal("75"),  Decimal("0.25"), Decimal("100"), Decimal("25")),
        (Decimal("60"),  Decimal("0.4"),  Decimal("100"), Decimal("40")),
        (Decimal("80"),  Decimal("0.2"),  Decimal("100"), Decimal("20")),
    ]
    for cost, m, exp_sell, exp_profit in cases:
        sell = sell_from_cost(cost, m)
        assert sell == exp_sell, f"sell wrong for cost={cost} m={m}"
        assert sell - cost == exp_profit, f"profit wrong for cost={cost} m={m}"
        assert (sell - cost) / sell == m, f"identity wrong for cost={cost} m={m}"


# ---------------------------------------------------------------------------
# Tier boundary semantics: [min, max) inclusive lower, exclusive upper
# ---------------------------------------------------------------------------

def test_tier_boundary_lower_inclusive():
    # cost == cost_min → that tier wins
    s = _stub_settings()
    p = price_line(Decimal("100"), "doors", _customer("retail"), s)
    # 100 is the cost_min of the [100,500) tier (margin 0.4), NOT the [0,100) tier
    assert p.margin_pct == Decimal("0.4")


def test_tier_boundary_upper_exclusive():
    # cost just under cost_max → that tier wins
    s = _stub_settings()
    p = price_line(Decimal("99.99"), "doors", _customer("retail"), s)
    # 99.99 is in [0,100), margin 0.5
    assert p.margin_pct == Decimal("0.5")


def test_tier_open_ended_top():
    s = _stub_settings()
    p = price_line(Decimal("99999"), "doors", _customer("retail"), s)
    # third tier [500, None) catches it
    assert p.margin_pct == Decimal("0.25")


def test_tier_zero_cost_lower_bound_inclusive():
    s = _stub_settings()
    p = price_line(Decimal("0"), "doors", _customer("retail"), s)
    # 0 ∈ [0, 100), margin 0.5
    assert p.margin_pct == Decimal("0.5")


def test_tier_no_match_fails_loud():
    bad = PricingSettingsView(
        tier_sets={("doors", "retail"): [TierRow(Decimal("100"), Decimal("200"), Decimal("0.3"))]},
        volume_discount_enabled=False,
        volume_tiers=[],
        class_volume_enabled={"retail": True, "contractor": True, "wholesale": True},
    )
    # cost=50 doesn't match the [100,200) tier — must FAIL not silently default
    with pytest.raises(PricingConfigError, match="no tier matches"):
        price_line(Decimal("50"), "doors", _customer("retail"), bad)


def test_tier_overlapping_fails_loud():
    """Two tiers covering the same cost must error, not pick one."""
    overlapping = [
        TierRow(Decimal("0"), Decimal("100"), Decimal("0.3")),
        TierRow(Decimal("50"), Decimal("150"), Decimal("0.4")),
    ]
    with pytest.raises(PricingConfigError, match="overlapping"):
        _find_tier(overlapping, Decimal("75"), where="t")


def test_missing_tier_set_fails_loud():
    s = _stub_settings()
    # category that wasn't seeded
    with pytest.raises(PricingConfigError, match="no tier set"):
        price_line(Decimal("100"), "nonsense_category", _customer("retail"), s)


def test_empty_tier_set_fails_loud():
    bad = PricingSettingsView(
        tier_sets={("doors", "retail"): []},
        volume_discount_enabled=False, volume_tiers=[],
        class_volume_enabled={"retail": True, "contractor": True, "wholesale": True},
    )
    with pytest.raises(PricingConfigError, match="empty"):
        price_line(Decimal("100"), "doors", _customer("retail"), bad)


# ---------------------------------------------------------------------------
# Resolution-order precedence
# ---------------------------------------------------------------------------

def test_line_override_beats_customer_override():
    s = _stub_settings()
    cust = _customer("retail", override=Decimal("0.4"))
    p = price_line(
        Decimal("100"), "doors", cust, s,
        line_margin_override=Decimal("0.6"),
    )
    assert p.margin_pct == Decimal("0.6")
    assert p.source == "line_override"


def test_customer_override_beats_tier():
    s = _stub_settings()
    cust = _customer("retail", override=Decimal("0.7"))
    p = price_line(Decimal("100"), "doors", cust, s)
    assert p.margin_pct == Decimal("0.7")
    assert p.source == "customer_override"


def test_wholesale_class_hits_wholesale_tier():
    s = _stub_settings()
    p = price_line(Decimal("100"), "doors", _customer("wholesale"), s)
    # wholesale doors flat 0.15
    assert p.margin_pct == Decimal("0.15")
    assert p.source == "wholesale_tier"


def test_contractor_class_uses_tier_source():
    s = _stub_settings()
    p = price_line(Decimal("100"), "doors", _customer("contractor"), s)
    assert p.margin_pct == Decimal("0.2")
    # source is "tier" for retail+contractor; only wholesale is its own source
    assert p.source == "tier"


def test_unset_class_defaults_to_retail():
    s = _stub_settings()
    p = price_line(Decimal("100"), "doors", _customer(None), s)
    assert p.margin_pct == Decimal("0.4")  # retail [100,500) tier
    assert p.source == "tier"


def test_invalid_class_fails_loud():
    with pytest.raises(PricingConfigError, match="not one of"):
        _resolve_class(_customer("vip"))  # type: ignore[arg-type]


def test_line_override_validates_margin():
    s = _stub_settings()
    with pytest.raises(PricingConfigError, match="infinite sell"):
        price_line(
            Decimal("100"), "doors", _customer("retail"), s,
            line_margin_override=Decimal("1.0"),
        )


def test_customer_override_validates_margin():
    s = _stub_settings()
    with pytest.raises(PricingConfigError, match="negative"):
        price_line(
            Decimal("100"), "doors",
            _customer("retail", override=Decimal("-0.1")), s,
        )


# ---------------------------------------------------------------------------
# Engine output correctness
# ---------------------------------------------------------------------------

def test_price_line_returns_correct_profit():
    s = _stub_settings()
    p = price_line(Decimal("100"), "doors", _customer("retail"), s)
    # margin 0.4 → sell = 100/0.6 = 166.66...
    assert p.sell == Decimal("100") / Decimal("0.6")
    assert p.profit == p.sell - Decimal("100")
    assert p.cost == Decimal("100")


def test_price_line_rejects_non_decimal_cost():
    s = _stub_settings()
    with pytest.raises(PricingConfigError, match="must be Decimal"):
        price_line(100.0, "doors", _customer("retail"), s)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Estimate totals
# ---------------------------------------------------------------------------

def test_price_estimate_basic_sums():
    """Two lines, no volume discount. Sums must equal hand-computed totals."""
    s = _stub_settings()
    cust = _customer("retail")
    lines = [
        EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("2")),
        EstimateLineInput(cost=Decimal("40"),  pricing_category="parts", quantity=Decimal("3")),
    ]
    t = price_estimate(lines, cust, s)
    # Line 1: cost 100, margin 0.4 → sell 100/0.6 = 166.666... × 2 = 333.333...
    expected_sell_1 = (Decimal("100") / Decimal("0.6")) * Decimal("2")
    # Line 2: cost 40 in [0,50) margin 0.6 → sell 40/0.4 = 100 × 3 = 300
    expected_sell_2 = (Decimal("40") / Decimal("0.4")) * Decimal("3")
    assert t.subtotal_sell_pre_discount == expected_sell_1 + expected_sell_2
    assert t.subtotal_cost == Decimal("100") * Decimal("2") + Decimal("40") * Decimal("3")
    assert t.volume_discount_pct == Decimal("0")
    assert t.volume_discount_amount == Decimal("0")
    assert t.subtotal_sell == t.subtotal_sell_pre_discount  # no discount applied
    assert t.profit == t.subtotal_sell - t.subtotal_cost


def test_price_estimate_blended_margin_correct():
    """Blended margin == single-line margin when only one line.
    Cost 120 ∈ [100,500) tier (margin 0.4) → sell = 120/0.6 = 200 (exact),
    profit 80, blended 80/200 = 0.4 (exact)."""
    s = _stub_settings()
    lines = [
        EstimateLineInput(cost=Decimal("120"), pricing_category="doors", quantity=Decimal("1")),
    ]
    t = price_estimate(lines, _customer("retail"), s)
    assert t.subtotal_sell == Decimal("200")
    assert t.profit == Decimal("80")
    assert t.blended_margin_pct == Decimal("0.4")


def test_price_estimate_zero_quantity_fails_loud():
    s = _stub_settings()
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("0"))]
    with pytest.raises(PricingConfigError, match="quantity must be > 0"):
        price_estimate(lines, _customer("retail"), s)


# ---------------------------------------------------------------------------
# Customer rolling-volume discount (Sprint 1.0.6)
# ---------------------------------------------------------------------------

def test_volume_discount_master_disabled_no_discount():
    """Master toggle off → no discount even with qualifying customer + tier."""
    s = _stub_settings(volume_enabled=False, volume_tiers=[
        VolumeTierRow(Decimal("0"), None, Decimal("0.1")),
    ])
    cust = _customer("retail", rolling_volume=Decimal("500000"))
    lines = [EstimateLineInput(cost=Decimal("1000"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.volume_discount_pct == Decimal("0")


def test_volume_discount_class_disabled_no_discount():
    """Per-class toggle off for retail → no discount even at high volume."""
    s = _stub_settings(
        volume_enabled=True,
        volume_tiers=[VolumeTierRow(Decimal("100000"), None, Decimal("0.04"))],
        class_enabled={"retail": False, "contractor": True, "wholesale": True},
    )
    cust = _customer("retail", rolling_volume=Decimal("500000"))
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.volume_discount_pct == Decimal("0")


def test_volume_discount_below_lowest_tier_no_discount_no_error():
    """Customer with $50k rolling volume, lowest tier at $100k → no discount, no crash."""
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("100000"), None, Decimal("0.02")),
    ])
    cust = _customer("retail", rolling_volume=Decimal("50000"))
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.volume_discount_pct == Decimal("0")


def test_volume_discount_applied_reduces_sell_and_profit():
    """Cliff: customer at $100k → 2% discount applied to entire estimate.

    Cost 120 ∈ [100,500) doors retail tier (margin 0.4) → sell = 120/0.6 = 200.
    Customer rolling volume $100k crosses 2% tier; discount $4, post-sell $196,
    profit $196-$120 = $76. Pre-discount profit was $80; drop is exactly $4.
    """
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("100000"), Decimal("300000"), Decimal("0.02")),
        VolumeTierRow(Decimal("300000"), None, Decimal("0.04")),
    ])
    cust = _customer("retail", rolling_volume=Decimal("100000"))
    lines = [EstimateLineInput(cost=Decimal("120"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.subtotal_sell_pre_discount == Decimal("200")
    assert t.volume_discount_pct == Decimal("0.02")
    assert t.volume_discount_amount == Decimal("4.00")
    assert t.subtotal_sell == Decimal("196.00")
    assert t.profit == Decimal("76.00")
    pre_profit = Decimal("200") - Decimal("120")  # 80
    assert pre_profit - t.profit == t.volume_discount_amount


def test_volume_discount_wholesale_class_qualifies():
    """Doug 2026-04-25: wholesale customer doing real volume gets discount on
    top of their already-lower wholesale margin. Confirms class doesn't auto-
    exempt — admin's class_enabled flag is the only gate."""
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("500000"), None, Decimal("0.03")),  # 3% above $500k
    ])
    # Wholesale doors tier flat 15% margin: cost 200 → sell = 200/0.85 ≈ 235.29
    cust = _customer("wholesale", rolling_volume=Decimal("800000"))
    lines = [EstimateLineInput(cost=Decimal("200"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.volume_discount_pct == Decimal("0.03")
    assert t.volume_discount_amount > Decimal("0")


def test_volume_discount_higher_tier_when_volume_exceeds():
    """Customer at $500k → matches the >$300k tier (4%), not the [100k,300k] tier."""
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("100000"), Decimal("300000"), Decimal("0.02")),
        VolumeTierRow(Decimal("300000"), None, Decimal("0.04")),
    ])
    cust = _customer("retail", rolling_volume=Decimal("500000"))
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.volume_discount_pct == Decimal("0.04")


def test_volume_discount_overlapping_tiers_fails_loud():
    """Customer rolling volume in the overlap zone → engine fails loud."""
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("0"), Decimal("200000"), Decimal("0.02")),
        VolumeTierRow(Decimal("100000"), Decimal("300000"), Decimal("0.03")),
    ])
    cust = _customer("retail", rolling_volume=Decimal("150000"))
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("1"))]
    with pytest.raises(PricingConfigError, match="overlapping volume tiers"):
        price_estimate(lines, cust, s)


def test_volume_discount_invalid_pct_fails_loud():
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("0"), None, Decimal("1.5")),  # 150% off — bogus
    ])
    cust = _customer("retail", rolling_volume=Decimal("100000"))
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("1"))]
    with pytest.raises(PricingConfigError, match="out of"):
        price_estimate(lines, cust, s)


def test_volume_discount_zero_volume_no_discount():
    """Customer with no payment history (or no cache) → discount is 0."""
    s = _stub_settings(volume_enabled=True, volume_tiers=[
        VolumeTierRow(Decimal("100000"), None, Decimal("0.02")),
    ])
    cust = _customer("retail", rolling_volume=Decimal("0"))
    lines = [EstimateLineInput(cost=Decimal("100"), pricing_category="doors", quantity=Decimal("1"))]
    t = price_estimate(lines, cust, s)
    assert t.volume_discount_pct == Decimal("0")


# ---------------------------------------------------------------------------
# Decimal precision: no float coercion anywhere
# ---------------------------------------------------------------------------

def test_no_float_drift_through_chain():
    """Long chain of operations should remain exact Decimal arithmetic."""
    s = _stub_settings()
    cost = Decimal("123.45")
    p = price_line(cost, "doors", _customer("retail"), s)
    # margin 0.4 → sell = 123.45 / 0.6
    expected_sell = Decimal("123.45") / Decimal("0.6")
    assert p.sell == expected_sell
    # Recompute margin from sell+cost — must equal input margin EXACTLY
    assert (p.sell - cost) / p.sell == Decimal("0.4")
