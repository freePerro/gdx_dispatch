"""ADR-015 Slice 1 — no-code custom catalog types.

A CustomCatalog with product_class='custom' carries its own field_schema; items
store user-defined values in `attributes` (JSON), coerced against that schema.
No typed SQL table, no migration, no deploy.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import catalog as catalog_router
from gdx_dispatch.routers.catalog import (
    DEFAULT_PRICING_SETTINGS,
    CatalogCreateIn,
    CatalogItemCreateIn,
    CatalogItemPatchIn,
)


def _mock_request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": "tenant-test"}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
    return {"user_id": "user-1", "role": "admin", "tenant_id": "tenant-test"}


# Representative cross-industry schema (HVAC), exercising several field types.
_HVAC_SCHEMA = [
    {"label": "Tonnage", "name": "tonnage", "type": "number"},
    {"label": "SEER", "name": "seer", "type": "number", "required": True},
    {"label": "Refrigerant", "name": "refrigerant", "type": "select",
     "options": ["R-410A", "R-32"]},
    {"label": "Energy Star", "name": "energy_star", "type": "checkbox"},
]


def _make_custom_catalog(db, schema=None):
    return catalog_router.create_catalog(
        CatalogCreateIn(
            name="HVAC Units",
            source_system="manual",
            product_class="custom",
            field_schema=schema if schema is not None else _HVAC_SCHEMA,
        ),
        _mock_request(),
        _user(),
        db,
    )


# ── schema validation ──────────────────────────────────────────────────────

def test_create_custom_catalog_returns_cleaned_schema(db_session):
    catalog = _make_custom_catalog(db_session)
    assert catalog["product_class"] == "custom"
    schema = catalog["field_schema"]
    names = [f["name"] for f in schema]
    assert names == ["tonnage", "seer", "refrigerant", "energy_star"]
    # required defaulted to False where omitted, preserved where set
    by_name = {f["name"]: f for f in schema}
    assert by_name["seer"]["required"] is True
    assert by_name["tonnage"]["required"] is False
    assert by_name["refrigerant"]["options"] == ["R-410A", "R-32"]


def test_custom_catalog_requires_at_least_one_field():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="Empty", product_class="custom", field_schema=[])
    with pytest.raises(ValueError):
        CatalogCreateIn(name="Empty", product_class="custom")  # field_schema=None


def test_unknown_field_type_rejected():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="X", product_class="custom",
                        field_schema=[{"label": "Bad", "name": "bad", "type": "rocket"}])


def test_duplicate_field_name_rejected():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="X", product_class="custom", field_schema=[
            {"label": "A", "name": "dup", "type": "text"},
            {"label": "B", "name": "dup", "type": "text"},
        ])


def test_unsafe_field_name_rejected():
    for bad in ("__proto__", "1leading", "has space", "x.y", ""):
        with pytest.raises(ValueError):
            CatalogCreateIn(name="X", product_class="custom",
                            field_schema=[{"label": "L", "name": bad, "type": "text"}])


def test_field_name_is_normalized_to_lowercase():
    payload = CatalogCreateIn(name="X", product_class="custom",
                              field_schema=[{"label": "Mixed", "name": "Upper", "type": "text"}])
    assert payload.field_schema[0]["name"] == "upper"


def test_select_without_options_rejected():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="X", product_class="custom",
                        field_schema=[{"label": "S", "name": "s", "type": "select"}])


def test_non_custom_catalog_drops_field_schema(db_session):
    # field_schema sent on a parts catalog is forced empty (built-ins use the
    # frontend registry, not data-driven fields).
    payload = CatalogCreateIn(name="Parts", product_class="parts",
                              field_schema=[{"label": "X", "name": "x", "type": "text"}])
    assert payload.field_schema == []
    catalog = catalog_router.create_catalog(payload, _mock_request(), _user(), db_session)
    assert catalog["field_schema"] == []


# ── item attribute round-trip ──────────────────────────────────────────────

def test_custom_item_coerces_and_persists_attributes(db_session):
    catalog = _make_custom_catalog(db_session)
    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(
            sku="AC-3T",
            name="3-Ton AC",
            cost=1200.0,
            price=2400.0,
            attributes={
                "tonnage": "3",          # str → float
                "seer": 16,              # int → float
                "refrigerant": "R-410A",
                "energy_star": 1,        # truthy → bool
                "bogus": "ignored",      # unknown key dropped
            },
        ),
        _mock_request(),
        _user(),
        db_session,
    )
    assert item["product_class"] == "custom"
    attrs = item["attributes"]
    assert attrs["tonnage"] == 3.0 and isinstance(attrs["tonnage"], float)
    assert attrs["seer"] == 16.0
    assert attrs["refrigerant"] == "R-410A"
    assert attrs["energy_star"] is True
    assert "bogus" not in attrs

    # Read-back through the list endpoint surfaces attributes too.
    listing = catalog_router.list_catalog_items(
        UUID(catalog["id"]), search=None, page=1, per_page=25,
        _=_user(), db=db_session,
    )
    assert listing["items"][0]["attributes"]["refrigerant"] == "R-410A"


def test_custom_item_patch_merges_attributes(db_session):
    catalog = _make_custom_catalog(db_session)
    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(name="Unit", cost=100.0, price=200.0,
                            attributes={"tonnage": 2, "refrigerant": "R-32"}),
        _mock_request(), _user(), db_session,
    )
    patched = catalog_router.patch_catalog_item(
        UUID(catalog["id"]), UUID(item["id"]),
        CatalogItemPatchIn(attributes={"refrigerant": "R-410A", "seer": 18}),
        _mock_request(), _user(), db_session,
    )
    attrs = patched["attributes"]
    assert attrs["refrigerant"] == "R-410A"   # overwritten
    assert attrs["seer"] == 18.0              # added
    assert attrs["tonnage"] == 2.0            # untouched, preserved


def test_parts_item_has_empty_attributes(db_session):
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(name="Parts", source_system="manual"),
        _mock_request(), _user(), db_session,
    )
    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(name="Bracket", cost=5.0, price=10.0,
                            attributes={"tonnage": 99}),  # ignored: not custom
        _mock_request(), _user(), db_session,
    )
    assert item["attributes"] == {}
