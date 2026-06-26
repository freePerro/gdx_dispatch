"""ADR-015 Slice 2 — pluggable catalog pricing strategies.

Strategies turn cost → retail when an item is saved with no price. 'manual'
(default) keeps the entered price (back-compat); other strategies auto-price.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import pricing_strategies as ps
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import catalog as catalog_router
from gdx_dispatch.routers.catalog import (
    DEFAULT_PRICING_SETTINGS,
    CatalogCreateIn,
    CatalogImportIn,
    CatalogItemCreateIn,
)


def _mock_request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": "tenant-test"}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    catalog_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _user() -> dict[str, str]:
    return {"user_id": "u", "role": "admin", "tenant_id": "tenant-test"}


# ── pure strategy math ─────────────────────────────────────────────────────

def test_builtin_strategy_math():
    assert ps.compute_price("manual", 100) is None
    assert ps.compute_price("keystone", 100) == Decimal("200")
    assert ps.compute_price("markup_50", 100) == Decimal("150.0")
    assert ps.compute_price("margin_50", 100) == Decimal("200")  # 100 / (1 - 0.5)


def test_negative_and_missing_cost_return_none():
    assert ps.compute_price("keystone", None) is None
    assert ps.compute_price("keystone", -5) is None


def test_unknown_strategy_falls_back_to_default_manual():
    # get_strategy falls back to 'manual', which is passthrough (None).
    assert ps.compute_price("does-not-exist", 100) is None


def test_register_pack_strategy_and_compute():
    ps.register_pack_strategy("hvac_markup_40", "HVAC 40%", "markup", {"pct": 0.4})
    assert ps.is_known("hvac_markup_40")
    assert ps.compute_price("hvac_markup_40", 100) == Decimal("140.0")
    # Declarative config wins even if the id is unknown to the registry.
    assert ps.compute_price("whatever", 100, config={"kind": "multiplier", "params": {"factor": 3}}) == Decimal("300")


def test_pack_strategy_cannot_shadow_builtin():
    with pytest.raises(ValueError):
        ps.register_pack_strategy("keystone", "x", "multiplier", {"factor": 9})


def test_list_strategies_includes_builtins():
    ids = {s["id"] for s in ps.list_strategies()}
    assert {"manual", "keystone", "markup_50", "margin_50"} <= ids


# ── strategy applied through the catalog router ─────────────────────────────

def _make_catalog(db, strategy="manual", config=None):
    return catalog_router.create_catalog(
        CatalogCreateIn(name=f"Cat {strategy}", source_system="manual",
                        product_class="parts", pricing_strategy=strategy,
                        pricing_config=config),
        _mock_request(), _user(), db,
    )


def test_catalog_persists_pricing_strategy(db_session):
    cat = _make_catalog(db_session, "keystone")
    assert cat["pricing_strategy"] == "keystone"


def test_item_autopriced_by_keystone_when_price_blank(db_session):
    cat = _make_catalog(db_session, "keystone")
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P1", name="Widget", cost=100.0),  # no price
        _mock_request(), _user(), db_session,
    )
    assert item["price"] == pytest.approx(200.0)


def test_manual_strategy_keeps_entered_or_cost_price(db_session):
    cat = _make_catalog(db_session, "manual")
    # no price + manual → falls back to cost (pre-ADR-015 behavior)
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P2", name="Bracket", cost=50.0),
        _mock_request(), _user(), db_session,
    )
    assert item["price"] == pytest.approx(50.0)


def test_explicit_price_overrides_strategy(db_session):
    cat = _make_catalog(db_session, "keystone")
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P3", name="Priced", cost=100.0, price=175.0),
        _mock_request(), _user(), db_session,
    )
    assert item["price"] == pytest.approx(175.0)  # not 200


def test_declarative_pricing_config_on_catalog(db_session):
    cat = _make_catalog(db_session, "pack_markup", config={"kind": "markup", "params": {"pct": 0.4}})
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P4", name="HVAC", cost=1000.0),
        _mock_request(), _user(), db_session,
    )
    assert item["price"] == pytest.approx(1400.0)


def test_bulk_import_applies_strategy(db_session):
    # Write-path consistency: CSV/JSON import prices cost-only rows like the form.
    cat = _make_catalog(db_session, "keystone")
    res = catalog_router.bulk_import_catalog_items(
        UUID(cat["id"]),
        CatalogImportIn(format="json", items=[{"name": "Imported", "cost": 100}]),
        _mock_request(), _user(), db_session,
    )
    assert res["imported"] == 1
    listing = catalog_router.list_catalog_items(
        UUID(cat["id"]), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )
    assert listing["items"][0]["price"] == pytest.approx(200.0)


def test_zero_price_policy_sees_strategy_computed_price(db_session, monkeypatch):
    # The reorder means enforce_save_pricing runs AFTER the strategy, so the
    # zero-price gate sees the real computed retail (200), not the blank 0.
    import gdx_dispatch.modules.catalog_policy as policy
    seen: dict = {}
    monkeypatch.setattr(policy, "enforce_save_pricing",
                        lambda tid, *, price: (seen.update(price=price), True)[1])
    cat = _make_catalog(db_session, "keystone")
    catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(name="Widget", cost=100.0),  # blank price
        _mock_request(), _user(), db_session,
    )
    assert seen["price"] == pytest.approx(200.0)


def test_unknown_strategy_rejected_at_validation():
    # Non-built-in id with no config is rejected deterministically (not via the
    # volatile pack registry).
    with pytest.raises(ValueError):
        CatalogCreateIn(name="x", pricing_strategy="bogus-strategy")


def test_bad_pricing_config_kind_rejected():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="x", pricing_strategy="manual",
                        pricing_config={"kind": "rocket", "params": {}})
