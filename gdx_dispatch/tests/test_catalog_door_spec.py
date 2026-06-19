"""Slice 1 — typed catalogs / Class Table Inheritance round-trip.

Verifies that a CustomCatalog with product_class='door' persists DoorSpec
install attributes alongside the spine row and serializes them back on read.
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


def test_create_door_catalog_carries_product_class(db_session):
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(name="Custom Doors", source_system="manual", product_class="door"),
        _mock_request(),
        _user(),
        db_session,
    )
    assert catalog["product_class"] == "door"


def test_default_product_class_is_parts_for_back_compat(db_session):
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(name="Old Style Catalog", source_system="manual"),
        _mock_request(),
        _user(),
        db_session,
    )
    assert catalog["product_class"] == "parts"


def test_door_item_round_trip_persists_install_attributes(db_session):
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(name="Custom Doors", source_system="manual", product_class="door"),
        _mock_request(),
        _user(),
        db_session,
    )
    spec = {
        "manufacturer": "CustomCo",
        "model_number": "CC-4050",
        "door_type": "sectional",
        "width": 192.0,
        "height": 84.0,
        "color": "white",
        "panel_style": "raised",
        "r_value": 18.4,
        "insulation_type": "polyurethane",
        "section_thickness_in": 2.0,
        "section_material": "steel",
        "window_option": "Y",
        "window_rows": 1,
        "window_type": "stockton",
        "finish_type": "smooth",
        "high_lift": "N",
    }
    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(
            sku="DOOR-CUSTOM-1",
            name="16x7 Custom Insulated Door",
            cost=850.0,
            price=1550.0,
            spec=spec,
        ),
        _mock_request(),
        _user(),
        db_session,
    )
    assert item["product_class"] == "door"
    assert item["spec"] is not None
    # Numeric coerced through Decimal — assert by float compare with tolerance
    assert item["spec"]["manufacturer"] == "CustomCo"
    assert item["spec"]["model_number"] == "CC-4050"
    assert float(item["spec"]["width"]) == pytest.approx(192.0)
    assert float(item["spec"]["r_value"]) == pytest.approx(18.4)
    assert item["spec"]["window_option"] == "Y"
    assert item["spec"]["panel_style"] == "raised"

    # Read-back via list endpoint surfaces the spec too
    listing = catalog_router.list_catalog_items(
        UUID(catalog["id"]),
        search=None,
        page=1,
        per_page=25,
        _=_user(),
        db=db_session,
    )
    assert len(listing["items"]) == 1
    assert listing["items"][0]["spec"]["manufacturer"] == "CustomCo"


def test_door_spec_patch_updates_install_attributes(db_session):
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(name="Custom Doors", source_system="manual", product_class="door"),
        _mock_request(),
        _user(),
        db_session,
    )
    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(
            sku="DOOR-1",
            name="Door 1",
            cost=500.0,
            price=900.0,
            spec={"color": "almond", "width": 96.0, "height": 84.0},
        ),
        _mock_request(),
        _user(),
        db_session,
    )
    patched = catalog_router.patch_catalog_item(
        UUID(catalog["id"]),
        UUID(item["id"]),
        CatalogItemPatchIn(spec={"color": "black", "panel_style": "carriage"}),
        _mock_request(),
        _user(),
        db_session,
    )
    assert patched["spec"]["color"] == "black"
    # Untouched fields preserved
    assert float(patched["spec"]["width"]) == pytest.approx(96.0)
    # Newly set field appears
    assert patched["spec"]["panel_style"] == "carriage"


def test_parts_catalog_ignores_spec_payload(db_session):
    catalog = catalog_router.create_catalog(
        CatalogCreateIn(name="Parts", source_system="manual"),  # default 'parts'
        _mock_request(),
        _user(),
        db_session,
    )
    item = catalog_router.add_catalog_item(
        UUID(catalog["id"]),
        CatalogItemCreateIn(
            sku="PART-1",
            name="Bracket",
            cost=5.0,
            price=10.0,
            spec={"width": 999.0},  # ignored: parts catalog has no DoorSpec
        ),
        _mock_request(),
        _user(),
        db_session,
    )
    assert item["product_class"] == "parts"
    assert "spec" not in item  # parts items don't include spec key


def test_invalid_product_class_rejected_at_validation():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="Nope", product_class="not-a-class")


# ─────────────────────────────────────────────────────────────────────────
# Virtual CHI catalogs (read-only feed-table surfacing)
# ─────────────────────────────────────────────────────────────────────────


def test_virtual_chi_doors_appears_when_table_has_rows(db_session):
    from gdx_dispatch.models.tenant_models import ChiDoorCatalog
    db_session.add(ChiDoorCatalog(
        sku="CHI-1607-WHT",
        brand="CHI",
        manufacturer="CHI",
        model_number="CHI-1607",
        door_type="sectional",
        description="16x7 white insulated",
        width=192,
        height=84,
        color="white",
        cost=620,
        sell_price=1100,
        is_active=True,
    ))
    db_session.commit()

    catalogs = catalog_router.list_catalogs(_user(), db_session)
    chi = [c for c in catalogs if c["id"] == catalog_router.VIRTUAL_CHI_DOORS_ID]
    assert chi, "CHI Doors virtual catalog missing from /api/catalogs"
    assert chi[0]["name"] == "CHI Doors"
    assert chi[0]["product_class"] == "door"
    assert chi[0]["read_only"] is True
    assert chi[0]["item_count"] == 1


def test_virtual_chi_doors_hidden_when_table_empty(db_session):
    catalogs = catalog_router.list_catalogs(_user(), db_session)
    assert not [c for c in catalogs if c["id"] == catalog_router.VIRTUAL_CHI_DOORS_ID]


def test_virtual_chi_doors_items_endpoint_returns_door_shape(db_session):
    from gdx_dispatch.models.tenant_models import ChiDoorCatalog
    db_session.add(ChiDoorCatalog(
        sku="CHI-1607-BLK",
        brand="CHI",
        manufacturer="CHI",
        model_number="CHI-1607",
        door_type="sectional",
        description="16x7 black insulated",
        width=192,
        height=84,
        color="black",
        r_value=18.4,
        panel_style="raised",
        cost=620,
        sell_price=1100,
        is_active=True,
    ))
    db_session.commit()

    result = catalog_router.list_catalog_items(
        UUID(catalog_router.VIRTUAL_CHI_DOORS_ID),
        search=None, page=1, per_page=25,
        _=_user(), db=db_session,
    )
    assert result["total"] == 1
    item = result["items"][0]
    assert item["sku"] == "CHI-1607-BLK"
    assert item["product_class"] == "door"
    assert item["read_only"] is True
    assert item["spec"]["color"] == "black"
    assert float(item["spec"]["width"]) == pytest.approx(192.0)
    assert float(item["spec"]["r_value"]) == pytest.approx(18.4)
    assert item["spec"]["panel_style"] == "raised"


def test_virtual_chi_doors_search_filters(db_session):
    from gdx_dispatch.models.tenant_models import ChiDoorCatalog
    db_session.add_all([
        ChiDoorCatalog(sku="CHI-A", brand="CHI", model_number="CHI-A",
                       description="white", is_active=True),
        ChiDoorCatalog(sku="CHI-B", brand="CHI", model_number="CHI-B",
                       description="black", is_active=True),
    ])
    db_session.commit()

    result = catalog_router.list_catalog_items(
        UUID(catalog_router.VIRTUAL_CHI_DOORS_ID),
        search="CHI-A", page=1, per_page=25,
        _=_user(), db=db_session,
    )
    skus = [i["sku"] for i in result["items"]]
    assert skus == ["CHI-A"]
