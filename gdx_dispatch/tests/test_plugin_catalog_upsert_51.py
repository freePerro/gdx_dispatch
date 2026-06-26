"""#51 — host-provided plugin→catalog upsert API (ADR-013 gap).

A plugin can now persist captured items into a browsable catalog through the
same pricing/vendor/attribute path the UI uses, deduping by SKU.
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
from gdx_dispatch.plugin_api.catalog import CatalogUpsertError, upsert_catalog_items
from gdx_dispatch.routers import catalog as catalog_router
from gdx_dispatch.routers.catalog import DEFAULT_PRICING_SETTINGS, CatalogCreateIn


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


def _user():
    return {"user_id": "u", "role": "admin", "tenant_id": "tenant-test"}


def _make_catalog(db, strategy="manual", product_class="parts", field_schema=None):
    return catalog_router.create_catalog(
        CatalogCreateIn(name=f"Cat {strategy}", source_system="manual",
                        product_class=product_class, pricing_strategy=strategy,
                        field_schema=field_schema),
        _mock_request(), _user(), db,
    )


def _items(db, catalog_id):
    listing = catalog_router.list_catalog_items(
        UUID(catalog_id), search=None, page=1, per_page=50, _=_user(), db=db,
    )
    return {i["sku"]: i for i in listing["items"]}


def test_upsert_creates_items_with_strategy_pricing_and_source(db_session):
    cat = _make_catalog(db_session, "keystone")
    res = upsert_catalog_items(
        db_session, cat["id"],
        [{"sku": "CHI-2216", "name": "CHI Door 16x7", "cost": 100}],
        source="chi-pricing",
    )
    assert (res.created, res.updated) == (1, 0)
    item = _items(db_session, cat["id"])["CHI-2216"]
    assert item["price"] == pytest.approx(200.0)   # keystone applied (not cost)
    assert item["vendor"] == "chi-pricing"          # source tag


def test_upsert_dedupes_by_sku_and_reprices(db_session):
    cat = _make_catalog(db_session, "keystone")
    upsert_catalog_items(db_session, cat["id"], [{"sku": "S1", "name": "Door", "cost": 100}])
    res = upsert_catalog_items(db_session, cat["id"], [{"sku": "S1", "name": "Door v2", "cost": 150}])
    assert (res.created, res.updated) == (0, 1)
    items = _items(db_session, cat["id"])
    assert len(items) == 1
    assert items["S1"]["name"] == "Door v2"
    assert items["S1"]["price"] == pytest.approx(300.0)  # repriced from new cost


def test_upsert_rejects_virtual_catalog(db_session):
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_DOORS_ID
    with pytest.raises(CatalogUpsertError):
        upsert_catalog_items(db_session, VIRTUAL_CHI_DOORS_ID, [{"sku": "X", "name": "X"}])


def test_upsert_rejects_missing_catalog(db_session):
    with pytest.raises(CatalogUpsertError):
        upsert_catalog_items(db_session, UUID(int=0), [{"sku": "X", "name": "X"}])


def test_upsert_custom_catalog_coerces_attributes(db_session):
    schema = [{"label": "Width", "name": "width", "type": "number"}]
    cat = _make_catalog(db_session, product_class="custom", field_schema=schema)
    upsert_catalog_items(
        db_session, cat["id"],
        [{"sku": "D1", "name": "Door", "cost": 50, "attributes": {"width": "16", "junk": "x"}}],
    )
    item = _items(db_session, cat["id"])["D1"]
    assert item["attributes"]["width"] == 16.0
    assert "junk" not in item["attributes"]
