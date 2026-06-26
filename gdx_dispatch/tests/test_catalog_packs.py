"""ADR-015 Slice 3 — Catalog Packs.

A pack (an ADR-013 plugin) contributes catalog types + declarative pricing as
DATA. The core surfaces pack types via the plugin-host; creating a catalog from
one copies its schema + pricing so the catalog is self-contained (no pack code in
the core process at pricing time).
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import pricing_strategies as ps
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.plugin_api import PluginManifest
from gdx_dispatch.routers import catalog as catalog_router
from gdx_dispatch.routers.catalog import (
    DEFAULT_PRICING_SETTINGS,
    CatalogCreateIn,
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


# Fake plugin-host /api/plugins payload (one HVAC pack).
_FAKE_PLUGINS = [{
    "key": "hvac",
    "name": "HVAC Catalog Pack",
    "catalog_types": [{
        "key": "hvac_unit",
        "label": "HVAC Units",
        "field_schema": [
            {"name": "tonnage", "label": "Tonnage", "type": "number"},
            {"name": "refrigerant", "label": "Refrigerant", "type": "select", "options": ["R-410A", "R-32"]},
        ],
        "pricing_strategy": {"id": "hvac_markup_40", "label": "HVAC Markup 40%",
                             "kind": "markup", "params": {"pct": 0.4}},
    }],
    "pricing_strategies": [],
}]


class _FakeResp:
    def __init__(self, data): self._data = data
    def raise_for_status(self): pass
    def json(self): return self._data


# ── manifest contract ──────────────────────────────────────────────────────

def test_manifest_accepts_catalog_pack_fields():
    m = PluginManifest(
        key="hvac", name="HVAC Pack",
        catalog_types=({"key": "hvac_unit", "label": "HVAC Units", "field_schema": []},),
        pricing_strategies=({"id": "x", "label": "X", "kind": "markup", "params": {"pct": 0.1}},),
    )
    assert m.catalog_types[0]["key"] == "hvac_unit"


def test_manifest_rejects_catalog_type_without_key():
    with pytest.raises(ValueError):
        PluginManifest(key="b", name="Bad", catalog_types=({"label": "No Key"},))


def test_manifest_rejects_pricing_strategy_without_kind():
    with pytest.raises(ValueError):
        PluginManifest(key="b", name="Bad", pricing_strategies=({"id": "x", "label": "X"},))


# ── pack-types endpoint (plugin-host stubbed) ──────────────────────────────

def test_list_pack_types_surfaces_and_registers(monkeypatch):
    monkeypatch.setattr(catalog_router.httpx, "get", lambda *a, **k: _FakeResp(_FAKE_PLUGINS))
    types = catalog_router.list_pack_catalog_types(_user())
    assert len(types) == 1
    t = types[0]
    assert t["key"] == "hvac_unit"
    assert t["plugin"] == "hvac"
    assert t["pricing_strategy"] == "hvac_markup_40"
    assert t["pricing_config"] == {"kind": "markup", "params": {"pct": 0.4}}
    # the declarative strategy is now registered for compute/listing
    assert ps.is_known("hvac_markup_40")


def test_list_pack_types_resilient_when_host_down(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(catalog_router.httpx, "get", _boom)
    assert catalog_router.list_pack_catalog_types(_user()) == []


# ── deterministic validation (no dependency on the volatile pack registry) ──

def test_pack_strategy_id_without_config_is_rejected():
    # A non-built-in id with no declarative spec is rejected the same on every
    # worker — we do NOT resolve it from the lazily-populated pack registry.
    with pytest.raises(ValueError):
        CatalogCreateIn(
            name="x", product_class="custom",
            field_schema=[{"name": "a", "label": "A", "type": "text"}],
            pricing_strategy="hvac_markup_40",  # even if registered elsewhere
        )


def test_pack_strategy_with_config_is_accepted():
    p = CatalogCreateIn(
        name="x", product_class="custom",
        field_schema=[{"name": "a", "label": "A", "type": "text"}],
        pricing_strategy="hvac_markup_40",
        pricing_config={"kind": "markup", "params": {"pct": 0.4}},
    )
    assert p.pricing_strategy == "hvac_markup_40"
    assert p.pricing_config["kind"] == "markup"


# ── create a catalog from a pack type (frontend copies schema+pricing) ─────

def test_create_from_pack_type_is_self_contained_and_prices(db_session):
    pack_type = _FAKE_PLUGINS[0]["catalog_types"][0]
    ps_spec = pack_type["pricing_strategy"]
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(
            name="My HVAC", product_class="custom",
            field_schema=pack_type["field_schema"],
            pricing_strategy=ps_spec["id"],
            pricing_config={"kind": ps_spec["kind"], "params": ps_spec["params"]},
        ),
        _mock_request(), _user(), db_session,
    )
    assert catalog["pricing_strategy"] == "hvac_markup_40"
    assert [f["name"] for f in catalog["field_schema"]] == ["tonnage", "refrigerant"]

    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(sku="AC-3T", name="3-Ton AC", cost=1000.0,
                            attributes={"tonnage": 3, "refrigerant": "R-410A"}),
        _mock_request(), _user(), db_session,
    )
    # markup 40% of cost 1000 → 1400, computed in-core from the copied config
    assert item["price"] == pytest.approx(1400.0)
    assert item["attributes"]["refrigerant"] == "R-410A"


# ── the real reference pack (if importable) ────────────────────────────────

def test_reference_hvac_pack_manifest_shape():
    pkg = pytest.importorskip("gdx_plugin_hvac")
    m = pkg.manifest
    assert m.key == "hvac"
    assert m.catalog_types[0]["key"] == "hvac_unit"
    assert m.pricing_strategies[0]["kind"] == "markup"
