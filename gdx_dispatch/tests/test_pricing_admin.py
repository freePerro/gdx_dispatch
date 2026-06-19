"""Sprint 1.0.5 Phase 4 — admin tier-set CRUD tests.

Direct handler tests bypass FastAPI dependencies (auth + tenant binding) so
they isolate the pricing-engine business logic. Exercises happy paths +
fail-loud validation paths (overlapping tiers, open-ended-not-top, bad pct).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from gdx_dispatch.models.pricing_engine import (
    CustomerVolumeDiscountTier,
    MarginTier,
    PricingClassSettings,
    PricingSettings,
    PricingTierSet,
    seed_default_pricing,
)
from gdx_dispatch.routers.pricing_admin import (
    ClassSettingIn,
    ClassSettingsReplaceIn,
    PreviewIn,
    PreviewLineIn,
    SettingsPatchIn,
    TierIn,
    TierSetReplaceIn,
    VolumeTierIn,
    VolumeTiersReplaceIn,
    get_settings,
    list_tier_sets,
    patch_settings,
    preview_pricing,
    replace_class_settings,
    replace_tiers,
    replace_volume_tiers,
)


@pytest.fixture
def seeded_db(tenant_db):
    seed_default_pricing(tenant_db)
    return tenant_db


def _stub_user(role="admin"):
    return {"sub": "u-1", "role": role, "user_id": "u-1"}


def _stub_request():
    """Mimic minimal Request shape audit logging touches."""
    req = MagicMock()
    req.state.tenant = {"id": "t-test"}
    req.headers = {}
    req.client = MagicMock(host="127.0.0.1")
    return req


# ── list_tier_sets ────────────────────────────────────────────────────────────


def test_list_tier_sets_returns_all_15_seeded(seeded_db):
    out = list_tier_sets(_=_stub_user(), db=seeded_db)
    assert len(out) == 15  # 5 categories × 3 classes
    # Each entry has id, category, class, active, tiers
    for entry in out:
        assert "id" in entry
        assert entry["pricing_category"] in ("doors", "openers", "parts", "labor", "other")
        assert entry["pricing_class"] in ("retail", "contractor", "wholesale")
        assert isinstance(entry["tiers"], list)
        assert len(entry["tiers"]) >= 2


# ── replace_tiers — happy + fail-loud ────────────────────────────────────────


def test_replace_tiers_happy_path(seeded_db):
    """Replace doors/retail tiers with a fresh set; verify DB matches."""
    ts = (
        seeded_db.query(PricingTierSet)
        .filter_by(pricing_category="doors", pricing_class="retail")
        .one()
    )
    new_tiers = TierSetReplaceIn(tiers=[
        TierIn(cost_min=0, cost_max=200, margin_pct=0.55),
        TierIn(cost_min=200, cost_max=None, margin_pct=0.30),
    ])
    out = replace_tiers(set_id=ts.id, payload=new_tiers, request=_stub_request(), user=_stub_user(), db=seeded_db)
    assert len(out["tiers"]) == 2
    assert out["tiers"][0]["margin_pct"] == 0.55
    assert out["tiers"][1]["cost_max"] is None

    # Verify in DB
    rows = seeded_db.query(MarginTier).filter_by(tier_set_id=ts.id).order_by(MarginTier.sort_order).all()
    assert len(rows) == 2
    assert float(rows[0].margin_pct) == 0.55


def test_replace_tiers_404_on_unknown_set(seeded_db):
    from uuid import uuid4
    with pytest.raises(HTTPException) as ex:
        replace_tiers(
            set_id=uuid4(),
            payload=TierSetReplaceIn(tiers=[TierIn(cost_min=0, cost_max=100, margin_pct=0.3)]),
            request=_stub_request(), user=_stub_user(), db=seeded_db,
        )
    assert ex.value.status_code == 404


def test_replace_tiers_rejects_overlapping_ranges(seeded_db):
    ts = seeded_db.query(PricingTierSet).first()
    bad = TierSetReplaceIn(tiers=[
        TierIn(cost_min=0, cost_max=100, margin_pct=0.5),
        TierIn(cost_min=50, cost_max=200, margin_pct=0.4),  # overlaps prev
    ])
    with pytest.raises(HTTPException) as ex:
        replace_tiers(set_id=ts.id, payload=bad, request=_stub_request(), user=_stub_user(), db=seeded_db)
    assert ex.value.status_code == 422
    assert "overlap" in ex.value.detail.lower()


def test_replace_tiers_rejects_open_ended_not_top(seeded_db):
    ts = seeded_db.query(PricingTierSet).first()
    bad = TierSetReplaceIn(tiers=[
        TierIn(cost_min=0, cost_max=None, margin_pct=0.5),  # open-ended at bottom
        TierIn(cost_min=100, cost_max=200, margin_pct=0.3),
    ])
    with pytest.raises(HTTPException) as ex:
        replace_tiers(set_id=ts.id, payload=bad, request=_stub_request(), user=_stub_user(), db=seeded_db)
    assert ex.value.status_code == 422
    assert "open-ended" in ex.value.detail.lower()


def test_replace_tiers_rejects_inverted_bounds(seeded_db):
    ts = seeded_db.query(PricingTierSet).first()
    bad = TierSetReplaceIn(tiers=[
        TierIn(cost_min=200, cost_max=100, margin_pct=0.3),  # max < min
    ])
    with pytest.raises(HTTPException) as ex:
        replace_tiers(set_id=ts.id, payload=bad, request=_stub_request(), user=_stub_user(), db=seeded_db)
    assert ex.value.status_code == 422


# ── settings + volume discount ────────────────────────────────────────────────


def test_get_settings_returns_seeded_singleton(seeded_db):
    out = get_settings(_=_stub_user(), db=seeded_db)
    assert out["volume_discount_enabled"] is False
    assert out["volume_tiers"] == []


def test_get_settings_lazy_seeds_when_missing(tenant_db):
    """No seed run beforehand → get_settings must seed lazily, not 500."""
    # Don't call seed_default_pricing
    out = get_settings(_=_stub_user(), db=tenant_db)
    assert out["volume_discount_enabled"] is False
    # Lazy seed populated all 15 sets
    assert tenant_db.query(PricingTierSet).count() == 15


def test_patch_settings_toggles_volume_discount(seeded_db):
    out = patch_settings(
        payload=SettingsPatchIn(volume_discount_enabled=True),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    assert out["volume_discount_enabled"] is True


def test_replace_volume_tiers_happy_path(seeded_db):
    out = replace_volume_tiers(
        payload=VolumeTiersReplaceIn(tiers=[
            VolumeTierIn(volume_min_12mo=100_000, volume_max_12mo=300_000, discount_pct=0.02),
            VolumeTierIn(volume_min_12mo=300_000, volume_max_12mo=None, discount_pct=0.04),
        ]),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    assert len(out["volume_tiers"]) == 2
    assert out["volume_tiers"][1]["discount_pct"] == 0.04


def test_replace_volume_tiers_rejects_overlap(seeded_db):
    bad = VolumeTiersReplaceIn(tiers=[
        VolumeTierIn(volume_min_12mo=0, volume_max_12mo=200_000, discount_pct=0.02),
        VolumeTierIn(volume_min_12mo=100_000, volume_max_12mo=400_000, discount_pct=0.04),
    ])
    with pytest.raises(HTTPException) as ex:
        replace_volume_tiers(payload=bad, request=_stub_request(), user=_stub_user(), db=seeded_db)
    assert ex.value.status_code == 422


def test_replace_volume_tiers_empty_clears(seeded_db):
    """Empty list = no volume discount tiers (admin clearing them)."""
    # First add some
    replace_volume_tiers(
        payload=VolumeTiersReplaceIn(tiers=[
            VolumeTierIn(volume_min_12mo=0, volume_max_12mo=None, discount_pct=0.02),
        ]),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    s = seeded_db.query(PricingSettings).first()
    assert seeded_db.query(CustomerVolumeDiscountTier).filter_by(settings_id=s.id).count() == 1

    # Now clear
    replace_volume_tiers(
        payload=VolumeTiersReplaceIn(tiers=[]),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    assert seeded_db.query(CustomerVolumeDiscountTier).filter_by(settings_id=s.id).count() == 0


# ── per-class settings (Sprint 1.0.6) ────────────────────────────────────────


def test_get_settings_returns_seeded_class_settings(seeded_db):
    out = get_settings(_=_stub_user(), db=seeded_db)
    assert "class_settings" in out
    classes = {c["pricing_class"]: c["rolling_volume_discount_enabled"] for c in out["class_settings"]}
    assert classes == {"retail": True, "contractor": True, "wholesale": True}


def test_replace_class_settings_disables_retail(seeded_db):
    """Doug's example: turn it on for wholesale only."""
    out = replace_class_settings(
        payload=ClassSettingsReplaceIn(classes=[
            ClassSettingIn(pricing_class="retail", rolling_volume_discount_enabled=False),
            ClassSettingIn(pricing_class="contractor", rolling_volume_discount_enabled=False),
            ClassSettingIn(pricing_class="wholesale", rolling_volume_discount_enabled=True),
        ]),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    classes = {c["pricing_class"]: c["rolling_volume_discount_enabled"] for c in out["class_settings"]}
    assert classes == {"retail": False, "contractor": False, "wholesale": True}


def test_replace_class_settings_rejects_duplicate_class(seeded_db):
    with pytest.raises(HTTPException) as ex:
        replace_class_settings(
            payload=ClassSettingsReplaceIn(classes=[
                ClassSettingIn(pricing_class="retail", rolling_volume_discount_enabled=True),
                ClassSettingIn(pricing_class="retail", rolling_volume_discount_enabled=False),
            ]),
            request=_stub_request(), user=_stub_user(), db=seeded_db,
        )
    assert ex.value.status_code == 422


# ── preview ──────────────────────────────────────────────────────────────────


def test_preview_engine_returns_correct_totals(seeded_db):
    """Preview a 1-line estimate end-to-end. Numbers must match engine math."""
    out = preview_pricing(
        payload=PreviewIn(
            lines=[PreviewLineIn(cost=200, pricing_category="doors", quantity=1)],
            pricing_class="retail",
        ),
        _=_stub_user(),
        db=seeded_db,
    )
    # Cost 200 ∈ doors retail [100,500) tier with margin 0.5 → sell 400
    assert out["subtotal_cost"] == 200.0
    assert out["subtotal_sell"] == 400.0
    assert out["profit"] == 200.0
    assert out["blended_margin_pct"] == 0.5
    assert out["lines"][0]["source"] == "tier"


def test_preview_with_volume_discount(seeded_db):
    # Enable + add a tier keyed on customer rolling volume (Sprint 1.0.6)
    patch_settings(payload=SettingsPatchIn(volume_discount_enabled=True),
                   request=_stub_request(), user=_stub_user(), db=seeded_db)
    replace_volume_tiers(
        payload=VolumeTiersReplaceIn(tiers=[
            VolumeTierIn(volume_min_12mo=100_000, volume_max_12mo=None, discount_pct=0.1),
        ]),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    # Preview with hypothetical customer at $250k rolling volume — qualifies
    out = preview_pricing(
        payload=PreviewIn(
            lines=[PreviewLineIn(cost=200, pricing_category="doors", quantity=1)],
            pricing_class="retail",
            customer_rolling_volume=250_000,
        ),
        _=_stub_user(),
        db=seeded_db,
    )
    # Pre: sell 400; customer volume $250k crosses $100k tier → 10% off → 360 sell
    assert out["subtotal_sell_pre_discount"] == 400.0
    assert out["volume_discount_pct"] == 0.1
    assert out["volume_discount_amount"] == 40.0


def test_preview_below_volume_threshold_no_discount(seeded_db):
    """Customer at $50k rolling volume, lowest tier $100k → no discount."""
    patch_settings(payload=SettingsPatchIn(volume_discount_enabled=True),
                   request=_stub_request(), user=_stub_user(), db=seeded_db)
    replace_volume_tiers(
        payload=VolumeTiersReplaceIn(tiers=[
            VolumeTierIn(volume_min_12mo=100_000, volume_max_12mo=None, discount_pct=0.1),
        ]),
        request=_stub_request(), user=_stub_user(), db=seeded_db,
    )
    out = preview_pricing(
        payload=PreviewIn(
            lines=[PreviewLineIn(cost=200, pricing_category="doors", quantity=1)],
            pricing_class="retail",
            customer_rolling_volume=50_000,
        ),
        _=_stub_user(),
        db=seeded_db,
    )
    assert out["volume_discount_pct"] == 0.0
    assert out["subtotal_sell"] == 400.0  # no discount → equals pre-discount
    assert out["profit"] == 200.0  # sell 400 - cost 200


def test_preview_bad_category_returns_409(seeded_db):
    with pytest.raises(HTTPException) as ex:
        preview_pricing(
            payload=PreviewIn(
                lines=[PreviewLineIn(cost=100, pricing_category="nonsense", quantity=1)],
                pricing_class="retail",
            ),
            _=_stub_user(),
            db=seeded_db,
        )
    assert ex.value.status_code == 409
    assert "no tier set" in ex.value.detail.lower()
