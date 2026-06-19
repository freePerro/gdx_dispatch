"""Integration tests — pricing engine against a real (in-memory) tenant DB.

Sprint 1.0.5. Verifies:
- seeder produces the expected DB state
- `hydrate_settings_from_db` correctly reconstructs PricingSettingsView
- door_catalog/price endpoint logic produces correct outputs end-to-end

Uses the `tenant_db` fixture from conftest.py (SQLite in-memory).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from gdx_dispatch.models.pricing_engine import (
    MarginTier,
    PricingClassSettings,
    PricingSettings,
    PricingTierSet,
    seed_default_pricing,
)
from gdx_dispatch.services.pricing_engine import (
    CustomerView,
    PricingConfigError,
    hydrate_settings_from_db,
    price_line,
)


# ---------------------------------------------------------------------------
# Seeder against a real DB
# ---------------------------------------------------------------------------


def test_seeder_populates_expected_rows(tenant_db):
    """Seeder writes 1 settings row + 15 tier sets + 3 class-settings rows."""
    # tenant_db fixture already ran TenantBase.metadata.create_all
    seed_default_pricing(tenant_db)
    assert tenant_db.query(PricingSettings).count() == 1
    assert tenant_db.query(PricingTierSet).count() == 15
    # Default seed tier counts: retail 4, contractor 3, wholesale 2 → 9 per category
    assert tenant_db.query(MarginTier).count() == 5 * 9
    # Sprint 1.0.6: per-class toggles, one per enum value, default enabled
    assert tenant_db.query(PricingClassSettings).count() == 3
    assert all(
        row.rolling_volume_discount_enabled
        for row in tenant_db.query(PricingClassSettings).all()
    )


def test_seeder_idempotent_on_real_db(tenant_db):
    """Re-running seeder doesn't duplicate."""
    seed_default_pricing(tenant_db)
    seed_default_pricing(tenant_db)
    assert tenant_db.query(PricingTierSet).count() == 15
    assert tenant_db.query(MarginTier).count() == 45
    assert tenant_db.query(PricingClassSettings).count() == 3


# ---------------------------------------------------------------------------
# Hydration: ORM → engine view
# ---------------------------------------------------------------------------


def test_hydrate_settings_returns_all_seeded_sets(tenant_db):
    seed_default_pricing(tenant_db)
    settings = hydrate_settings_from_db(tenant_db)
    # 5 categories × 3 classes
    assert len(settings.tier_sets) == 15
    for cat in ("doors", "openers", "parts", "labor", "other"):
        for cls in ("retail", "contractor", "wholesale"):
            assert (cat, cls) in settings.tier_sets
            assert len(settings.tier_sets[(cat, cls)]) >= 2  # at least 2 tier rows
    assert settings.volume_discount_enabled is False
    assert settings.volume_tiers == []
    # Sprint 1.0.6: all classes default-enabled at seed
    assert settings.class_volume_enabled == {
        "retail": True, "contractor": True, "wholesale": True,
    }


def test_hydrate_fails_loud_when_settings_missing(tenant_db):
    """Tenant without seed → engine refuses, doesn't silently default."""
    # Don't call seeder — DB is bare
    with pytest.raises(PricingConfigError, match="no PricingSettings row"):
        hydrate_settings_from_db(tenant_db)


# ---------------------------------------------------------------------------
# End-to-end: seeded tier values flow through to price_line correctly
# ---------------------------------------------------------------------------


def test_e2e_retail_doors_uses_seed_tiers(tenant_db):
    """Cost 200 ∈ [100,500) seed tier (margin 0.5) → sell 400, profit 200.

    Verifies seed → hydrate → price math chain with no surprises.
    """
    seed_default_pricing(tenant_db)
    settings = hydrate_settings_from_db(tenant_db)
    p = price_line(
        cost=Decimal("200"),
        pricing_category="doors",
        customer=CustomerView(pricing_class="retail", margin_override_pct=None),
        settings=settings,
    )
    assert p.margin_pct == Decimal("0.5")
    assert p.sell == Decimal("400")
    assert p.profit == Decimal("200")
    assert p.source == "tier"


def test_e2e_wholesale_class_hits_wholesale_tier_set(tenant_db):
    """Wholesale doors seed: flat 0.20 in [0,1000), 0.15 in [1000, ∞)."""
    seed_default_pricing(tenant_db)
    settings = hydrate_settings_from_db(tenant_db)
    p = price_line(
        cost=Decimal("500"),
        pricing_category="doors",
        customer=CustomerView(pricing_class="wholesale", margin_override_pct=None),
        settings=settings,
    )
    assert p.margin_pct == Decimal("0.2")  # [0,1000) tier
    assert p.source == "wholesale_tier"


def test_e2e_unknown_category_fails_loud(tenant_db):
    seed_default_pricing(tenant_db)
    settings = hydrate_settings_from_db(tenant_db)
    with pytest.raises(PricingConfigError, match="no tier set"):
        price_line(
            cost=Decimal("100"),
            pricing_category="not_a_real_category",
            customer=CustomerView(pricing_class="retail", margin_override_pct=None),
            settings=settings,
        )


def test_e2e_user_edited_tier_persists_through_hydrate(tenant_db):
    """Simulate an admin edit: change retail doors tier-1 margin in DB,
    confirm hydrate reads the new value (not the seed)."""
    seed_default_pricing(tenant_db)
    # Find the retail doors tier set + its first tier
    ts = (
        tenant_db.query(PricingTierSet)
        .filter_by(pricing_category="doors", pricing_class="retail")
        .one()
    )
    tier1 = (
        tenant_db.query(MarginTier)
        .filter_by(tier_set_id=ts.id)
        .order_by(MarginTier.sort_order)
        .first()
    )
    # Default seed tier-1 margin is 0.6; admin lowers it to 0.45
    tier1.margin_pct = Decimal("0.45")
    tenant_db.commit()

    settings = hydrate_settings_from_db(tenant_db)
    p = price_line(
        cost=Decimal("50"),  # ∈ [0,100)
        pricing_category="doors",
        customer=CustomerView(pricing_class="retail", margin_override_pct=None),
        settings=settings,
    )
    assert p.margin_pct == Decimal("0.45")  # admin-edited value, NOT seed 0.6
