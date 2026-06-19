from __future__ import annotations

from copy import deepcopy
from io import StringIO
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import catalog as catalog_router
from gdx_dispatch.routers import pricing as pricing_router
from gdx_dispatch.routers.catalog import (
    CatalogCreateIn,
    CatalogImportIn,
    CatalogItemCreateIn,
    CatalogItemPatchIn,
    QBSyncPullIn,
    QBSyncPushIn,
)
from gdx_dispatch.routers.pricing import DEFAULT_PRICING_SETTINGS, MarkupBatchIn, MarkupItemIn, PricingSettingsPatchIn


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    catalog_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _user() -> dict[str, str]:
    return {"user_id": "user-1", "role": "admin", "tenant_id": "tenant-test"}


def _mock_request():
    from types import SimpleNamespace
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": "tenant-test"}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


def _create_catalog(db: Session, name: str = "Primary Catalog", source: str = "manual") -> dict[str, object]:
    return catalog_router.create_catalog(
        CatalogCreateIn(name=name, source=source), _mock_request(), _user(), db
    )


def _create_item(db: Session, catalog_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sku": "SKU-100",
        "name": "Torsion Spring",
        "description": "2-inch spring",
        "cost": 100.0,
        "price": 130.0,
        "category": "parts",
        "active": True,
    }
    payload.update(overrides)
    return catalog_router.add_catalog_item(
        UUID(catalog_id), CatalogItemCreateIn(**payload), _mock_request(), _user(), db
    )


def test_calculate_sell_price_retail():
    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    data = pricing_router.calculate_sell_price(cost=100, customer_type="retail", _=_user())
    assert data["sell_price"] == pytest.approx(130.0)
    assert data["margin_pct"] == pytest.approx(0.30)


def test_calculate_sell_price_contractor():
    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    data = pricing_router.calculate_sell_price(cost=100, customer_type="contractor", _=_user())
    assert data["sell_price"] == pytest.approx(125.0)
    assert data["margin_pct"] == pytest.approx(0.25)


def test_part_tier_markup():
    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    data = pricing_router.calculate_markup(MarkupBatchIn(items=[MarkupItemIn(cost=50)]), _=_user())
    assert len(data["items"]) == 1
    assert data["items"][0]["markup_pct"] == pytest.approx(1.0)
    assert data["items"][0]["sell_price"] == pytest.approx(100.0)


def test_volume_discount():
    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    data = pricing_router.calculate_sell_price(cost=1000, customer_type="retail", annual_spend=50000, _=_user())
    assert data["volume_discount_pct"] == pytest.approx(0.02)
    assert data["sell_price"] == pytest.approx(1274.0)


def test_pricing_settings_patch():
    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    patched = pricing_router.patch_pricing_settings(
        PricingSettingsPatchIn(labor_rates={"default": 95.0, "tech_overrides": {"tech-7": 110.0}}),
        _user(),
    )
    assert patched["labor_rates"]["default"] == pytest.approx(95.0)
    assert patched["labor_rates"]["tech_overrides"]["tech-7"] == pytest.approx(110.0)


def test_calculate_markup_batch_uses_tiers():
    pricing_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)
    result = pricing_router.calculate_markup(
        MarkupBatchIn(items=[MarkupItemIn(cost=50), MarkupItemIn(cost=300), MarkupItemIn(cost=900)]),
        _=_user(),
    )
    prices = [row["sell_price"] for row in result["items"]]
    assert prices == pytest.approx([100.0, 450.0, 1170.0])


def test_catalog_crud(db_session: Session):
    catalog = _create_catalog(db_session, name="Warehouse", source="chi")
    created_item = _create_item(db_session, str(catalog["id"]))

    listed = catalog_router.list_catalog_items(UUID(str(catalog["id"])), search=None, page=1, per_page=25, _=_user(), db=db_session)  # list_catalog_items is GET, signature unchanged
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created_item["id"]

    patched = catalog_router.patch_catalog_item(
        UUID(str(catalog["id"])),
        UUID(str(created_item["id"])),
        CatalogItemPatchIn(cost=150.0, price=210.0, active=False),
        _mock_request(),
        _user(),
        db_session,
    )
    assert patched["cost"] == pytest.approx(150.0)
    assert patched["price"] == pytest.approx(210.0)
    assert patched["active"] is False

    deleted = catalog_router.delete_catalog_item(
        UUID(str(catalog["id"])),
        UUID(str(created_item["id"])),
        _mock_request(),
        _user(),
        db_session,
    )
    assert deleted["deleted"] is True
    after = catalog_router.list_catalog_items(UUID(str(catalog["id"])), search=None, page=1, per_page=25, _=_user(), db=db_session)  # list_catalog_items is GET, signature unchanged
    assert after["total"] == 0


def test_catalog_items_search_and_pagination(db_session: Session):
    catalog = _create_catalog(db_session)
    for i in range(1, 8):
        _create_item(db_session, str(catalog["id"]), sku=f"SKU-{i}", name=f"Part {i}")

    page1 = catalog_router.list_catalog_items(UUID(str(catalog["id"])), search="Part", page=1, per_page=3, _=_user(), db=db_session)
    page2 = catalog_router.list_catalog_items(UUID(str(catalog["id"])), search="Part", page=2, per_page=3, _=_user(), db=db_session)
    assert page1["total"] == 7
    assert len(page1["items"]) == 3
    assert len(page2["items"]) == 3


def test_bulk_import_json(db_session: Session):
    catalog = _create_catalog(db_session, name="Imported")
    payload = CatalogImportIn(
        format="json",
        items=[
            {
                "sku": f"IMP-{i}",
                "name": f"Imported {i}",
                "description": "bulk",
                "cost": 10 + i,
                "price": 20 + i,
                "category": "parts",
            }
            for i in range(10)
        ],
    )
    result = catalog_router.bulk_import_catalog_items(
        UUID(str(catalog["id"])), payload, _mock_request(), _user(), db_session
    )
    assert result["imported"] == 10
    listed = catalog_router.list_catalog_items(UUID(str(catalog["id"])), search=None, page=1, per_page=50, _=_user(), db=db_session)
    assert listed["total"] == 10


def test_bulk_import_csv(db_session: Session):
    catalog = _create_catalog(db_session, name="CSV Import")
    out = StringIO()
    out.write("sku,name,description,cost,price,category\n")
    out.write("CSV-1,CSV Part 1,from csv,12.5,20.5,parts\n")
    out.write("CSV-2,CSV Part 2,from csv,20,30,parts\n")
    result = catalog_router.bulk_import_catalog_items(
        UUID(str(catalog["id"])),
        CatalogImportIn(format="csv", csv_data=out.getvalue()),
        _mock_request(),
        _user(),
        db_session,
    )
    assert result["imported"] == 2


def test_qb_item_link(db_session: Session):
    catalog = _create_catalog(db_session, source="qb")
    created = _create_item(db_session, str(catalog["id"]), qb_item_id="QB-101")
    assert created["qb_item_id"] == "QB-101"

    pull_result = catalog_router.qb_pull_sync(
        UUID(str(catalog["id"])),
        QBSyncPullIn(
            items=[
                {
                    "qb_item_id": "QB-101",
                    "sku": "SKU-100",
                    "name": "Torsion Spring Updated",
                    "description": "qb pull",
                    "cost": 115,
                    "price": 155,
                    "category": "parts",
                    "active": True,
                }
            ]
        ),
        _mock_request(),
        _user(),
        db_session,
    )
    assert pull_result["updated"] == 1

    pushed = catalog_router.qb_push_sync(
        UUID(str(catalog["id"])),
        QBSyncPushIn(create_missing=True),
        _mock_request(),
        _user(),
        db_session,
    )
    assert pushed["pushed"] >= 0


def test_qb_pull_creates_new_item(db_session: Session):
    catalog = _create_catalog(db_session, source="qb")
    pull_result = catalog_router.qb_pull_sync(
        UUID(str(catalog["id"])),
        QBSyncPullIn(
            items=[
                {
                    "qb_item_id": "QB-NEW",
                    "sku": "QB-NEW-SKU",
                    "name": "QB Item",
                    "description": "from qb",
                    "cost": 88,
                    "price": 120,
                    "category": "parts",
                    "active": True,
                }
            ]
        ),
        _mock_request(),
        _user(),
        db_session,
    )
    assert pull_result["created"] == 1


def test_catalog_source_validation():
    with pytest.raises(ValueError):
        CatalogCreateIn(name="Bad", source="invalid-source")


def test_list_catalog_items_404_for_missing(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        catalog_router.list_catalog_items(UUID(int=0), search=None, page=1, per_page=25, _=_user(), db=db_session)
    assert exc.value.status_code == 404


def test_router_module_gating_contract():
    # Catalog router stays gated on the inventory module.
    dependency_fn = catalog_router.router.dependencies[0].dependency
    closure_values = [cell.cell_contents for cell in (dependency_fn.__closure__ or [])]
    assert "inventory" in closure_values
    # Pricing router used to be un-gated; now requires the estimates module.
    # The old assertion `dependencies == []` encoded an insecure posture —
    # any authenticated user could hit pricing endpoints with no module
    # check at all. Fixed in the CLAUDE.md Build Rule sweep.
    assert len(pricing_router.router.dependencies) >= 2, "pricing router must have at least two dependencies"
    # dependencies[0] is bind_tenant_context; dependencies[1] is require_module("estimates")
    pricing_dep_fn = pricing_router.router.dependencies[1].dependency
    pricing_closures = [cell.cell_contents for cell in (pricing_dep_fn.__closure__ or [])]
    assert "estimates" in pricing_closures
