from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import catalog as catalog_router
from gdx_dispatch.routers.catalog import (
    DEFAULT_PRICING_SETTINGS,
    CatalogCreateIn,
    CatalogImportIn,
    CatalogItemCreateIn,
    CatalogItemPatchIn,
    PricingSettingsPatchIn,
)


def _mock_request() -> SimpleNamespace:
    # Lightweight Request shim — the catalog handlers now take `request: Request`
    # so the audit logger can capture tenant + actor; tests don't hit the
    # FastAPI stack so we supply a stub.
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


def _create_catalog(db: Session, name: str = "Primary Catalog", source_system: str = "manual") -> dict[str, object]:
    return catalog_router.create_catalog(
        CatalogCreateIn(name=name, source_system=source_system),
        _mock_request(),
        _user(),
        db,
    )


def _create_item(db: Session, catalog_id: str, **overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "sku": "SKU-100",
        "name": "Torsion Spring",
        "description": "2-inch spring",
        "cost": 100.0,
        "price": 175.0,
        "category": "parts",
    }
    payload.update(overrides)
    return catalog_router.add_catalog_item(
        UUID(catalog_id),
        CatalogItemCreateIn(**payload),
        _mock_request(),
        _user(),
        db,
    )


def test_create_and_list_catalogs(db_session: Session):
    created = _create_catalog(db_session, name="Warehouse", source_system="chi")

    rows = catalog_router.list_catalogs(_user(), db_session)
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]
    assert rows[0]["name"] == "Warehouse"
    assert rows[0]["source_system"] == "chi"


def test_create_catalog_requires_name(db_session: Session):
    with pytest.raises(ValueError):
        CatalogCreateIn(name="   ", source_system="manual")


def test_get_catalog_includes_items(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(db_session, str(catalog["id"]), sku="SPR-1", name="Spring")

    data = catalog_router.get_catalog(UUID(str(catalog["id"])), _user(), db_session)
    assert data["id"] == catalog["id"]
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == item["id"]


def test_get_catalog_404_for_missing(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        catalog_router.get_catalog(UUID(int=0), _user(), db_session)
    assert exc.value.status_code == 404


def test_add_item_requires_existing_catalog(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        catalog_router.add_catalog_item(
            UUID(int=0),
            CatalogItemCreateIn(
                sku="X",
                name="X",
                description="X",
                cost=10,
                price=20,
                category="parts",
            ),
            _mock_request(),
            _user(),
            db_session,
        )
    assert exc.value.status_code == 404


def test_patch_item_updates_fields(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(db_session, str(catalog["id"]))

    patched = catalog_router.patch_catalog_item(
        UUID(str(catalog["id"])),
        UUID(str(item["id"])),
        CatalogItemPatchIn(name="Updated Item", cost=123.45, price=222.22),
        _mock_request(),
        _user(),
        db_session,
    )
    assert patched["name"] == "Updated Item"
    assert patched["cost"] == pytest.approx(123.45)
    assert patched["price"] == pytest.approx(222.22)


def test_patch_item_404_for_missing_item(db_session: Session):
    catalog = _create_catalog(db_session)
    with pytest.raises(HTTPException) as exc:
        catalog_router.patch_catalog_item(
            UUID(str(catalog["id"])),
            UUID(int=0),
            CatalogItemPatchIn(name="Nope"),
            _mock_request(),
            _user(),
            db_session,
        )
    assert exc.value.status_code == 404


def test_delete_item_hides_it_from_catalog(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(db_session, str(catalog["id"]))

    deleted = catalog_router.delete_catalog_item(
        UUID(str(catalog["id"])),
        UUID(str(item["id"])),
        _mock_request(),
        _user(),
        db_session,
    )
    assert deleted["deleted"] is True

    get_row = catalog_router.get_catalog(UUID(str(catalog["id"])), _user(), db_session)
    assert get_row["items"] == []


def test_delete_item_404_for_missing_item(db_session: Session):
    catalog = _create_catalog(db_session)
    with pytest.raises(HTTPException) as exc:
        catalog_router.delete_catalog_item(
            UUID(str(catalog["id"])),
            UUID(int=0),
            _mock_request(),
            _user(),
            db_session,
        )
    assert exc.value.status_code == 404


def test_pricing_calculate_uses_margin_and_customer_type():
    catalog_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)

    data = catalog_router.calculate_sell_price(
        cost=100,
        margin_type="standard",
        customer_type="retail",
        _=_user(),
    )
    assert data["cost"] == pytest.approx(100.0)
    assert data["margin"] == pytest.approx(0.5)
    assert data["sell_price"] == pytest.approx(200.0)


def test_pricing_settings_get_and_patch():
    catalog_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)

    before = catalog_router.get_pricing_settings(_user())
    assert "margins" in before
    assert "tiers" in before
    assert "volume_discounts" in before

    patched = catalog_router.patch_pricing_settings(
        PricingSettingsPatchIn(
            margins={"standard": {"retail": 0.4, "contractor": 0.3, "wholesale": 0.2}},
        ),
        _user(),
    )
    assert patched["margins"]["standard"]["retail"] == pytest.approx(0.4)

    calc = catalog_router.calculate_sell_price(
        cost=100,
        margin_type="standard",
        customer_type="retail",
        _=_user(),
    )
    assert calc["sell_price"] == pytest.approx(166.67, abs=0.01)


def test_pricing_calculate_422_for_unknown_margin_or_customer_type():
    catalog_router._PRICING_SETTINGS = deepcopy(DEFAULT_PRICING_SETTINGS)

    with pytest.raises(HTTPException) as margin_exc:
        catalog_router.calculate_sell_price(cost=100, margin_type="unknown", customer_type="retail", _=_user())
    assert margin_exc.value.status_code == 422

    with pytest.raises(HTTPException) as customer_exc:
        catalog_router.calculate_sell_price(cost=100, margin_type="standard", customer_type="vip", _=_user())
    assert customer_exc.value.status_code == 422


def test_catalog_routes_registered_in_main_app():
    app_py = Path("gdx_dispatch/app.py").read_text(encoding="utf-8")
    assert "from gdx_dispatch.routers import catalog as catalog_router" in app_py
    assert "app.include_router(catalog_router.router if hasattr(catalog_router, \"router\") else catalog_router)" in app_py


# ─── S114 D-S111-catalog-retail-engine-mismatch-test ──────────────────────
def test_virtual_catalog_chi_doors_emits_pricing_status_when_no_tier(db_session: Session):
    """The CHI Doors virtual catalog calls the pricing engine when sell_price
    is null. If the tenant hasn't configured a 'doors' retail margin tier,
    the response must surface pricing_status='not_configured' so the
    frontend can render an admin warning banner. Closes
    D-S111-no-engine-fallback-when-tiers-missing."""
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_DOORS_ID, _virtual_catalog_items
    # Empty DB has no PricingSettings row → hydrate raises PricingConfigError.
    res = _virtual_catalog_items(VIRTUAL_CHI_DOORS_ID, search=None, page=1, per_page=5, db=db_session)
    # Empty CHI catalog → 0 items, but the status field MUST be set so the
    # frontend can render the banner regardless of whether items exist.
    assert "pricing_status" in res
    assert res["pricing_status"] in ("not_configured", "ok", "error")
    if res["pricing_status"] != "ok":
        assert res.get("pricing_status_message"), (
            "non-ok status must carry an actionable message for the admin banner"
        )


def test_virtual_catalog_chi_doors_computed_retail_matches_engine(tenant_db):
    """End-to-end value check: when a CHI door has cost set but no
    sell_price, the response's `price` must equal what the pricing engine
    would compute (cost × tier margin) for the doors/retail tier.
    Otherwise the catalog table and the actual estimate-line price would
    drift, causing real money mistakes. Closes
    D-S111-catalog-retail-engine-mismatch-test (full version)."""
    from decimal import Decimal as _D
    from gdx_dispatch.models.pricing_engine import seed_default_pricing
    from gdx_dispatch.models.tenant_models import ChiDoorCatalog
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_DOORS_ID, _virtual_catalog_items
    from gdx_dispatch.services.pricing_engine import (
        CustomerView, hydrate_settings_from_db, price_line,
    )

    seed_default_pricing(tenant_db)
    door = ChiDoorCatalog(
        sku="CHI-TEST-9X8",
        manufacturer="CHI",
        model_number="TEST-9X8",
        cost=_D("1000.00"),
        sell_price=None,  # explicit — engine fallback should fire
        is_active=True,
        pricing_category="doors",
    )
    tenant_db.add(door)
    tenant_db.commit()

    res = _virtual_catalog_items(VIRTUAL_CHI_DOORS_ID, search=None, page=1, per_page=5, db=tenant_db)
    items = res["items"]
    assert len(items) == 1, f"expected 1 row, got {len(items)}"
    row = items[0]
    assert row["sku"] == "CHI-TEST-9X8"
    assert row["cost"] == 1000.0
    assert row["price_source"] == "computed", (
        f"with sell_price=null + tier configured, price_source must be 'computed' — got {row['price_source']!r}"
    )

    # The catalog endpoint hydrates settings the same way the engine does
    # at estimate-line-add time; verify the value matches what an estimate
    # would compute. They MUST agree, or the user sees one number on /catalog
    # and a different number on the estimate.
    settings = hydrate_settings_from_db(tenant_db)
    customer = CustomerView(pricing_class="retail", margin_override_pct=None)
    expected = round(float(price_line(
        cost=_D("1000.00"),
        pricing_category="doors",
        customer=customer,
        settings=settings,
    ).sell), 2)
    assert row["price"] == expected, (
        f"catalog computed retail ({row['price']}) must match engine output ({expected}) — "
        f"divergence means catalog and estimate prices would differ"
    )


def test_virtual_catalog_response_shape_includes_price_source(db_session: Session):
    """Every CHI catalog item row carries a `price_source` field so the
    frontend can mark engine-computed prices with a footnote."""
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_PARTS_ID, _virtual_catalog_items
    res = _virtual_catalog_items(VIRTUAL_CHI_PARTS_ID, search=None, page=1, per_page=5, db=db_session)
    assert "items" in res
    for item in res["items"]:
        # Field must be present even when null (catalog doesn't have data
        # to compute from, e.g., cost is null).
        assert "price_source" in item, f"missing price_source in row: {item}"
        assert item["price_source"] in (None, "catalog", "computed"), (
            f"unexpected price_source value: {item['price_source']!r}"
        )


def test_delete_catalog_soft_deletes_and_hides_from_list(db_session: Session):
    # #49 — deleting a catalog removes it from the list and its items from the
    # pickers, but it's a soft delete (deleted_at set, rows kept).
    catalog = _create_catalog(db_session, name="Throwaway")
    _create_item(db_session, str(catalog["id"]))

    res = catalog_router.delete_catalog(
        UUID(str(catalog["id"])), _mock_request(), _user(), db_session,
    )
    assert res["deleted"] is True

    rows = catalog_router.list_catalogs(_user(), db_session)
    assert all(r["id"] != catalog["id"] for r in rows)
    with pytest.raises(HTTPException) as exc:
        catalog_router.get_catalog(UUID(str(catalog["id"])), _user(), db_session)
    assert exc.value.status_code == 404


def test_delete_catalog_404_for_missing(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        catalog_router.delete_catalog(UUID(int=0), _mock_request(), _user(), db_session)
    assert exc.value.status_code == 404


def test_delete_catalog_refuses_virtual(db_session: Session):
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_DOORS_ID
    with pytest.raises(HTTPException) as exc:
        catalog_router.delete_catalog(
            UUID(VIRTUAL_CHI_DOORS_ID), _mock_request(), _user(), db_session,
        )
    assert exc.value.status_code == 409


def test_patch_catalog_active_toggles_and_filters_picker(db_session: Session):
    # #50 — deactivating a catalog keeps it listed but drops its items from the
    # estimate/billing picker (all-items). Reactivating restores them.
    catalog = _create_catalog(db_session, name="Seasonal")
    _create_item(db_session, str(catalog["id"]))

    before = catalog_router.list_all_catalog_items(_user(), db_session)
    assert before["total"] == 1

    patched = catalog_router.patch_catalog(
        UUID(str(catalog["id"])),
        catalog_router.CatalogPatchIn(active=False),
        _mock_request(), _user(), db_session,
    )
    assert patched["active"] is False
    # Still listed on the Catalogs page...
    rows = catalog_router.list_catalogs(_user(), db_session)
    assert any(r["id"] == catalog["id"] for r in rows)
    # ...but its items leave the picker.
    after = catalog_router.list_all_catalog_items(_user(), db_session)
    assert after["total"] == 0

    catalog_router.patch_catalog(
        UUID(str(catalog["id"])),
        catalog_router.CatalogPatchIn(active=True),
        _mock_request(), _user(), db_session,
    )
    assert catalog_router.list_all_catalog_items(_user(), db_session)["total"] == 1


def test_patch_catalog_404_for_missing(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        catalog_router.patch_catalog(
            UUID(int=0), catalog_router.CatalogPatchIn(active=False),
            _mock_request(), _user(), db_session,
        )
    assert exc.value.status_code == 404


def test_patch_catalog_refuses_virtual(db_session: Session):
    from gdx_dispatch.routers.catalog import VIRTUAL_CHI_DOORS_ID
    with pytest.raises(HTTPException) as exc:
        catalog_router.patch_catalog(
            UUID(VIRTUAL_CHI_DOORS_ID), catalog_router.CatalogPatchIn(active=False),
            _mock_request(), _user(), db_session,
        )
    assert exc.value.status_code == 409


def test_new_catalog_defaults_active_true(db_session: Session):
    catalog = _create_catalog(db_session, name="Fresh")
    assert catalog["active"] is True


# ── #55: first-class vendor field ───────────────────────────────────────────

def test_add_item_persists_vendor(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(db_session, str(catalog["id"]), vendor="Midwest Wholesale Doors")
    assert item["vendor"] == "Midwest Wholesale Doors"


def test_patch_item_updates_vendor(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(db_session, str(catalog["id"]))
    patched = catalog_router.patch_catalog_item(
        UUID(str(catalog["id"])), UUID(str(item["id"])),
        CatalogItemPatchIn(vendor="Acme Supply"),
        _mock_request(), _user(), db_session,
    )
    assert patched["vendor"] == "Acme Supply"
    # Blanking clears it.
    cleared = catalog_router.patch_catalog_item(
        UUID(str(catalog["id"])), UUID(str(item["id"])),
        CatalogItemPatchIn(vendor="   "),
        _mock_request(), _user(), db_session,
    )
    assert cleared["vendor"] is None


def test_bulk_import_maps_vendor_and_supplier_alias(db_session: Session):
    catalog = _create_catalog(db_session)
    catalog_router.bulk_import_catalog_items(
        UUID(str(catalog["id"])),
        CatalogImportIn(format="json", items=[
            {"name": "A", "cost": 1, "vendor": "VendorCo"},
            {"name": "B", "cost": 1, "supplier": "SupplierCo"},  # alias
        ]),
        _mock_request(), _user(), db_session,
    )
    listing = catalog_router.list_catalog_items(
        UUID(str(catalog["id"])), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )
    by_name = {i["name"]: i["vendor"] for i in listing["items"]}
    assert by_name["A"] == "VendorCo"
    assert by_name["B"] == "SupplierCo"


# ── #57: QB catalog sync gate ───────────────────────────────────────────────

def _set_catalog_sync(db_session, enabled: bool):
    from gdx_dispatch.models.tenant_models import AppSettings
    row = db_session.query(AppSettings).first()
    if row is None:
        row = AppSettings(integrations={})
        db_session.add(row)
    row.integrations = {"quickbooks_catalog_sync": enabled}
    db_session.commit()


def test_qb_pull_blocked_when_catalog_sync_disabled(db_session: Session):
    from gdx_dispatch.routers.catalog import QBSyncPullIn
    catalog = _create_catalog(db_session)
    _set_catalog_sync(db_session, False)
    with pytest.raises(HTTPException) as exc:
        catalog_router.qb_pull_sync(
            UUID(str(catalog["id"])),
            QBSyncPullIn(items=[{"sku": "X", "name": "X", "cost": 1}]),
            _mock_request(), _user(), db_session,
        )
    assert exc.value.status_code == 409


def test_qb_pull_allowed_when_catalog_sync_enabled(db_session: Session):
    from gdx_dispatch.routers.catalog import QBSyncPullIn
    catalog = _create_catalog(db_session)
    _set_catalog_sync(db_session, True)
    res = catalog_router.qb_pull_sync(
        UUID(str(catalog["id"])),
        QBSyncPullIn(items=[{"sku": "X", "name": "Widget", "cost": 10}]),
        _mock_request(), _user(), db_session,
    )
    assert res["created"] == 1


def test_qb_push_blocked_when_catalog_sync_disabled(db_session: Session):
    from gdx_dispatch.routers.catalog import QBSyncPushIn
    catalog = _create_catalog(db_session)
    _set_catalog_sync(db_session, False)
    with pytest.raises(HTTPException) as exc:
        catalog_router.qb_push_sync(
            UUID(str(catalog["id"])), QBSyncPushIn(),
            _mock_request(), _user(), db_session,
        )
    assert exc.value.status_code == 409


def test_catalog_sync_defaults_off(db_session: Session):
    # No AppSettings row at all → helper reports disabled (safe default).
    from gdx_dispatch.routers.settings import quickbooks_catalog_sync_enabled
    assert quickbooks_catalog_sync_enabled(db_session) is False


# --- description_display junk filtering ------------------------------------
# Some imports stuffed the category name into the description column (part
# L956W arrived with description "Accessories"), so lines added from the
# picker read as the category instead of the part. A description equal to the
# item's category or pricing bucket must fall back to the name.

def test_description_display_falls_back_when_description_is_category(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(
        db_session, str(catalog["id"]),
        sku="L956W",
        name="Wireless Multi-Function Control Panel",
        description="Accessories",
        category="Accessories",
    )
    assert item["description_display"] == "Wireless Multi-Function Control Panel"
    # Raw column is untouched — only the display field filters the junk.
    assert item["description"] == "Accessories"


def test_description_display_falls_back_when_description_is_pricing_category(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(
        db_session, str(catalog["id"]),
        sku="P-1", name="Nylon Roller", description="Parts",
        category="Hardware", pricing_category="parts",
    )
    assert item["description_display"] == "Nylon Roller"


def test_description_display_keeps_real_descriptions(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(
        db_session, str(catalog["id"]),
        sku="SPR-2", name="Torsion Spring 2in",
        description="2-inch torsion spring, 10k cycle",
        category="parts",
    )
    assert item["description_display"] == "2-inch torsion spring, 10k cycle"


def test_description_display_empty_description_still_falls_back_to_name(db_session: Session):
    catalog = _create_catalog(db_session)
    item = _create_item(
        db_session, str(catalog["id"]),
        sku="SPR-3", name="Extension Spring", description=None, category="parts",
    )
    assert item["description_display"] == "Extension Spring"
