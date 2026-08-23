"""core.part_pricing — the ONE sell-price resolver for a captured part.

Contract pinned here:

1. The job's own priced parts row for a SKU wins. The office scopes and
   prices a job up front and may have adjusted that number for the customer;
   it outranks list price.
2. Bench inventory (``Part.unit_price`` via ``part_id``) is next — the
   original PR4 behaviour, unchanged.
3. The tenant catalog is reachable by SKU. This is the tier that never
   worked: a catalog-picked part carries no ``part_id`` by design, so the
   old ``part_row.unit_price``-only read returned NULL for exactly the parts
   the office had already priced.
4. **A catalog ``price`` is only believed when it is above cost.** 1,493 of
   1,843 priced rows in production carry ``price == cost`` (QuickBooks
   imports). Billing those at face value is a zero-margin sale. Cost goes
   through the tenant's margin engine instead.
5. Never invent a price: no match, or two rows disagreeing on one SKU,
   returns None and the office prices it.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.part_pricing import resolve_sell_price
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import (
    ChiPartsCatalog,
    CustomCatalog,
    CustomCatalogItem,
    Customer,
    Job,
    JobPartNeeded,
)
from gdx_dispatch.modules.inventory.models import Part

TENANT = "tenant-partprice"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        JobPartNeeded.__table__,
        Part.__table__,
        CustomCatalog.__table__,
        CustomCatalogItem.__table__,
        ChiPartsCatalog.__table__,
        PricingSettings.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Opener install",
        job_type="Service Call",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _catalog(db) -> CustomCatalog:
    cat = CustomCatalog(id=uuid4(), name="Midwest Wholesale")
    db.add(cat)
    db.commit()
    return cat


def _catalog_item(db, cat, *, sku, price, cost, category="parts", name="Item"):
    item = CustomCatalogItem(
        id=uuid4(),
        catalog_id=cat.id,
        sku=sku,
        name=name,
        price=Decimal(str(price)) if price is not None else None,
        cost=Decimal(str(cost)) if cost is not None else None,
        pricing_category=category,
        product_class="custom",
        active=True,
    )
    db.add(item)
    db.commit()
    return item


def _job_row(db, job, *, sku, price, source="request", status="needed"):
    row = JobPartNeeded(
        id=str(uuid4()),
        company_id=TENANT,
        job_id=str(job.id),
        part_name=f"part {sku}",
        sku=sku,
        quantity=1,
        status=status,
        source=source,
        unit_price=Decimal(str(price)) if price is not None else None,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# Tier 1 — the office's number for THIS job wins.
# ---------------------------------------------------------------------------
def test_job_scoped_price_beats_catalog_list_price(db) -> None:
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="2220L-7", price=536.00, cost=268.00)
    # The office put this part on the job at a negotiated 500.00.
    _job_row(db, job, sku="2220L-7", price=500.00)

    assert resolve_sell_price(db, job_id=str(job.id), sku="2220L-7") == Decimal("500.00")


def test_job_scoped_price_is_case_insensitive_on_sku(db) -> None:
    job = _job(db)
    _job_row(db, job, sku="OPENER-7", price=500.00)

    assert resolve_sell_price(db, job_id=str(job.id), sku="opener-7") == Decimal("500.00")


def test_job_rows_disagreeing_on_price_fall_through(db) -> None:
    """Two office rows, same SKU, different prices — a human settles it."""
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="2220L-7", price=536.00, cost=268.00)
    _job_row(db, job, sku="2220L-7", price=500.00)
    _job_row(db, job, sku="2220L-7", price=475.00)

    # Falls through to the catalog rather than picking one arbitrarily.
    assert resolve_sell_price(db, job_id=str(job.id), sku="2220L-7") == Decimal("536.00")


# ---------------------------------------------------------------------------
# Tier 2 — bench inventory, the original behaviour.
# ---------------------------------------------------------------------------
def test_inventory_part_id_still_prices(db) -> None:
    job = _job(db)
    part = Part(id=uuid4(), sku="SEAL-12", name="Bottom seal", unit_price=45.00, qty_on_hand=3)
    db.add(part)
    db.commit()

    assert resolve_sell_price(
        db, job_id=str(job.id), sku="SEAL-12", part_id=part.id
    ) == Decimal("45.00")


# ---------------------------------------------------------------------------
# Tier 3 — the catalog, the tier that never worked.
# ---------------------------------------------------------------------------
def test_catalog_sku_prices_without_any_part_id(db) -> None:
    """The regression that started all of this.

    A catalog-picked part carries no part_id (FK to parts.id), so the old
    inventory-only read captured NULL and the invoice silently lost it.
    """
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="979U", price=85.50, cost=42.75, name="Universal Keypad")

    assert resolve_sell_price(db, job_id=str(job.id), sku="979U", part_id=None) == Decimal("85.50")


def test_zero_margin_catalog_price_is_never_billed_as_sell(db) -> None:
    """The QuickBooks-import trap.

    price == cost is an import artefact, not a sell price. With no margin
    tiers configured the resolver must return None (office prices it) rather
    than bill a $2,207 door at cost.
    """
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="CHI-2294", price=2207.00, cost=2207.00, name="CHI 2294 10x8")

    resolved = resolve_sell_price(db, job_id=str(job.id), sku="CHI-2294")
    assert resolved != Decimal("2207.00"), "billed a door at cost — zero margin"
    assert resolved is None


def test_catalog_price_below_cost_is_not_trusted(db) -> None:
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="ODD-1", price=10.00, cost=99.00)

    assert resolve_sell_price(db, job_id=str(job.id), sku="ODD-1") is None


def test_inactive_and_deleted_catalog_items_are_ignored(db) -> None:
    job = _job(db)
    cat = _catalog(db)
    item = _catalog_item(db, cat, sku="GONE-1", price=99.00, cost=10.00)
    item.active = False
    db.commit()

    assert resolve_sell_price(db, job_id=str(job.id), sku="GONE-1") is None


# ---------------------------------------------------------------------------
# Never invent a price.
# ---------------------------------------------------------------------------
def test_unknown_sku_returns_none(db) -> None:
    job = _job(db)
    assert resolve_sell_price(db, job_id=str(job.id), sku="NOPE-404") is None


def test_blank_sku_returns_none(db) -> None:
    job = _job(db)
    assert resolve_sell_price(db, job_id=str(job.id), sku=None) is None
    assert resolve_sell_price(db, job_id=str(job.id), sku="   ") is None


def test_two_catalogs_disagreeing_on_one_sku_returns_none(db) -> None:
    job = _job(db)
    cat_a = _catalog(db)
    cat_b = CustomCatalog(id=uuid4(), name="Other")
    db.add(cat_b)
    db.commit()
    _catalog_item(db, cat_a, sku="DUP-1", price=100.00, cost=50.00)
    _catalog_item(db, cat_b, sku="DUP-1", price=140.00, cost=50.00)

    assert resolve_sell_price(db, job_id=str(job.id), sku="DUP-1") is None


def test_unpriced_job_rows_do_not_shadow_the_catalog(db) -> None:
    """A NULL-priced office row must not count as 'the office's number'."""
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="979U", price=85.50, cost=42.75)
    _job_row(db, job, sku="979U", price=None)

    assert resolve_sell_price(db, job_id=str(job.id), sku="979U") == Decimal("85.50")


# ---------------------------------------------------------------------------
# The zero-margin rescue: with tiers configured, cost becomes a real sell.
#
# Production carries 15 configured tier sets, so this — not the None above —
# is the live behaviour for the 1,493 import rows.
# ---------------------------------------------------------------------------
def test_zero_margin_catalog_cost_is_marked_up_by_the_engine(db) -> None:
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db)
    db.commit()

    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="CHI-2294", price=2207.00, cost=2207.00,
                  category="doors", name="CHI 2294 10x8")

    resolved = resolve_sell_price(db, job_id=str(job.id), sku="CHI-2294")
    assert resolved is not None, "tiers are configured — this must price"
    assert resolved > Decimal("2207.00"), (
        f"marked up to {resolved}, which is not above cost"
    )


def test_engine_markup_never_applies_to_labor_category(db) -> None:
    """Labor must never tier-markup — the 2026-05-07 $91k cascade."""
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db)
    db.commit()

    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="LABOR-1", price=100.00, cost=100.00, category="labor")

    assert resolve_sell_price(db, job_id=str(job.id), sku="LABOR-1") is None


# ---------------------------------------------------------------------------
# Duplicate DETECTION — never suppression.
#
# AUDIT-R1 (2026-07-07) ruled capture rows are never machine-merged: any
# automatic dedup undercounts or double-counts. Duplicates get reported to
# the office, which decides. This matters now that the rows carry prices.
# ---------------------------------------------------------------------------
def test_same_part_captured_twice_is_reported(db) -> None:
    from gdx_dispatch.core.part_pricing import duplicate_capture_groups

    job = _job(db)
    _job_row(db, job, sku="OPENER-7", price=536.00, source="mobile", status="used")
    _job_row(db, job, sku="OPENER-7", price=536.00, source="closeout", status="used")

    warnings = duplicate_capture_groups(db, str(job.id))
    assert len(warnings) == 1
    assert warnings[0]["times_captured"] == 2
    assert warnings[0]["sources"] == ["closeout", "mobile"]
    assert warnings[0]["sku"] == "OPENER-7"


def test_distinct_parts_are_not_reported_as_duplicates(db) -> None:
    from gdx_dispatch.core.part_pricing import duplicate_capture_groups

    job = _job(db)
    _job_row(db, job, sku="SPRING-9", price=120.00, source="mobile", status="used")
    _job_row(db, job, sku="ROLLER-2", price=18.00, source="closeout", status="used")

    assert duplicate_capture_groups(db, str(job.id)) == []


def test_differing_quantities_are_not_collapsed_into_one_warning(db) -> None:
    """Qty is part of the key, matching closeout_job's _billed_keys.

    Two rows for the same part at different quantities are the ambiguous
    case a human settles — they are not one duplicated part.
    """
    from gdx_dispatch.core.part_pricing import duplicate_capture_groups

    job = _job(db)
    a = _job_row(db, job, sku="ROLLER-2", price=18.00, source="mobile", status="used")
    b = _job_row(db, job, sku="ROLLER-2", price=18.00, source="closeout", status="used")
    a.quantity = 2
    b.quantity = 4
    db.commit()

    assert duplicate_capture_groups(db, str(job.id)) == []


def test_billed_and_dismissed_rows_are_not_reported(db) -> None:
    from gdx_dispatch.core.part_pricing import duplicate_capture_groups

    job = _job(db)
    _job_row(db, job, sku="OPENER-7", price=536.00, source="mobile", status="used")
    dismissed = _job_row(db, job, sku="OPENER-7", price=536.00, source="closeout", status="wont_bill")
    db.commit()

    assert duplicate_capture_groups(db, str(job.id)) == []
    dismissed.status = "used"
    dismissed.billed_invoice_id = None
    db.commit()
    assert len(duplicate_capture_groups(db, str(job.id))) == 1


def test_office_request_rows_are_not_duplicates_of_captures(db) -> None:
    """An office-scoped row and the tech's attestation of it are the normal
    shape of a job, not a duplicate: only capture rows are compared."""
    from gdx_dispatch.core.part_pricing import duplicate_capture_groups

    job = _job(db)
    _job_row(db, job, sku="OPENER-7", price=536.00, source="request", status="needed")
    _job_row(db, job, sku="OPENER-7", price=536.00, source="closeout", status="used")

    assert duplicate_capture_groups(db, str(job.id)) == []


# ---------------------------------------------------------------------------
# The feedback loop: the resolver must never read back its own writes.
#
# This module WRITES its results into job_parts_needed as capture rows. Tier 1
# reads that table. Without a source filter a machine-derived number is
# laundered into "the office's price" on the next capture.
# ---------------------------------------------------------------------------
def test_capture_rows_are_never_read_back_as_the_office_price(db) -> None:
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="979U", price=85.50, cost=42.75)
    # A previous capture wrote this row with a machine-derived price.
    _job_row(db, job, sku="979U", price=150.00, source="mobile", status="used")

    # The catalog, not the earlier machine write, must win.
    assert resolve_sell_price(db, job_id=str(job.id), sku="979U") == Decimal("85.50")


def test_closeout_and_van_rows_are_also_ignored_by_tier_one(db) -> None:
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="979U", price=85.50, cost=42.75)
    _job_row(db, job, sku="979U", price=999.00, source="closeout", status="used")
    _job_row(db, job, sku="979U", price=888.00, source="van", status="used")

    assert resolve_sell_price(db, job_id=str(job.id), sku="979U") == Decimal("85.50")


# ---------------------------------------------------------------------------
# price/cost edge cases.
# ---------------------------------------------------------------------------
def test_priced_item_with_no_cost_recorded_is_believed(db) -> None:
    """Refusing this would silently unprice legitimately-priced items."""
    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="NOCOST-1", price=75.00, cost=None)

    assert resolve_sell_price(db, job_id=str(job.id), sku="NOCOST-1") == Decimal("75.00")


def test_a_rejected_price_is_not_replaced_by_a_marked_up_cost(db) -> None:
    """price <= cost is a contradiction, not an absence.

    Marking the cost up here would bill ABOVE a price a human actually typed.
    """
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db)
    db.commit()

    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="STALE-1", price=50.00, cost=60.00)

    assert resolve_sell_price(db, job_id=str(job.id), sku="STALE-1") is None


# ---------------------------------------------------------------------------
# The customer's margin class must apply even when the caller omits it.
# ---------------------------------------------------------------------------
def test_customer_class_is_resolved_from_the_job_when_not_passed(db) -> None:
    """Mobile and van capture pass no customer_id.

    Defaulting those to 'retail' would over-bill every contractor and
    wholesale customer, so the resolver looks the customer up from the job.
    """
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db)
    db.commit()

    retail_cust = Customer(id=uuid4(), name="Retail Co", pricing_class="retail", company_id=TENANT)
    whole_cust = Customer(id=uuid4(), name="Wholesale Co", pricing_class="wholesale", company_id=TENANT)
    db.add_all([retail_cust, whole_cust])
    db.commit()

    retail_job = Job(customer_id=retail_cust.id, title="r", job_type="Service Call", company_id=TENANT)
    whole_job = Job(customer_id=whole_cust.id, title="w", job_type="Service Call", company_id=TENANT)
    db.add_all([retail_job, whole_job])
    db.commit()

    cat = _catalog(db)
    _catalog_item(db, cat, sku="MARKUP-1", price=100.00, cost=100.00, category="parts")

    retail_price = resolve_sell_price(db, job_id=str(retail_job.id), sku="MARKUP-1")
    db.info.pop("_part_pricing_settings", None)
    whole_price = resolve_sell_price(db, job_id=str(whole_job.id), sku="MARKUP-1")

    assert retail_price is not None and whole_price is not None
    assert whole_price < retail_price, (
        f"wholesale {whole_price} should undercut retail {retail_price} — "
        "the caller passed no customer_id, so the job had to supply it"
    )


# ---------------------------------------------------------------------------
# A price lookup must never be why a closeout fails.
# ---------------------------------------------------------------------------
def test_resolver_returns_none_instead_of_raising(monkeypatch) -> None:
    import gdx_dispatch.core.part_pricing as pp

    def boom(*a, **kw):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(pp, "_resolve_sell_price", boom)
    assert pp.resolve_sell_price(None, job_id="j", sku="X") is None


# ---------------------------------------------------------------------------
# sell_price_for_row — the SKU-suggest path shares the capture path's rules.
#
# LineItemEditor.addSelectedParts has always read `hit.price` from
# /parts-needed/sku-suggest to fill an unpriced part pulled onto an invoice.
# The endpoint never returned that field, so the fallback was dead code and
# every such pull landed at $0.
# ---------------------------------------------------------------------------
def test_suggest_row_pricing_matches_the_capture_path(db) -> None:
    from gdx_dispatch.core.part_pricing import sell_price_for_row

    job = _job(db)
    cat = _catalog(db)
    _catalog_item(db, cat, sku="979U", price=85.50, cost=42.75)

    from_capture = resolve_sell_price(db, job_id=str(job.id), sku="979U")
    from_suggest = sell_price_for_row(db, price=Decimal("85.50"), cost=Decimal("42.75"))
    assert from_capture == from_suggest == Decimal("85.50")


def test_suggest_row_never_offers_a_zero_margin_import_price(db) -> None:
    """The office must not be handed cost as if it were a sell price."""
    from gdx_dispatch.core.part_pricing import sell_price_for_row
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db)
    db.commit()

    offered = sell_price_for_row(
        db, price=Decimal("2207.00"), cost=Decimal("2207.00"), pricing_category="doors"
    )
    assert offered is not None
    assert offered > Decimal("2207.00")


def test_suggest_row_with_no_price_or_cost_is_none(db) -> None:
    from gdx_dispatch.core.part_pricing import sell_price_for_row

    assert sell_price_for_row(db, price=None, cost=None) is None
    assert sell_price_for_row(db, price=Decimal("0"), cost=Decimal("0")) is None


# ---------------------------------------------------------------------------
# Audit fix: tier 4 (CHI) must apply the same believability guard as tier 3,
# or the capture path and the suggest path return different numbers for the
# identical row -- the exact drift the shared helper exists to prevent.
# ---------------------------------------------------------------------------
def test_chi_zero_margin_row_is_not_billed_at_cost(db) -> None:
    job = _job(db)
    db.add(ChiPartsCatalog(
        id=uuid4(), sku="CHI-SPR-1", name="CHI spring",
        sell_price=Decimal("48.00"), cost=Decimal("48.00"), is_active=True,
    ))
    db.commit()

    # No tiers configured -> both paths must decline, not offer cost.
    assert resolve_sell_price(db, job_id=str(job.id), sku="CHI-SPR-1") is None


def test_chi_capture_and_suggest_paths_agree(db) -> None:
    from gdx_dispatch.core.part_pricing import sell_price_for_row
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db)
    db.commit()

    job = _job(db)
    db.add(ChiPartsCatalog(
        id=uuid4(), sku="CHI-SPR-2", name="CHI spring",
        sell_price=Decimal("48.00"), cost=Decimal("48.00"),
        pricing_category="parts", is_active=True,
    ))
    db.commit()

    from_capture = resolve_sell_price(db, job_id=str(job.id), sku="CHI-SPR-2")
    db.info.pop("_part_pricing_settings", None)
    from_suggest = sell_price_for_row(
        db, price=Decimal("48.00"), cost=Decimal("48.00"), pricing_category="parts"
    )
    assert from_capture is not None
    assert from_capture == from_suggest, (
        f"capture said {from_capture}, suggest said {from_suggest} — "
        "one money decision, two answers"
    )


# ---------------------------------------------------------------------------
# Audit fix A: the office's Add-from-Catalog posts the RAW catalog price
# column, which for QuickBooks imports equals cost. Storing it verbatim
# billed at cost AND taught tier 1 that cost was the agreed price for that
# SKU. add_part_needed now re-derives server-side.
# ---------------------------------------------------------------------------
def test_office_catalog_add_never_stores_cost_as_the_sell_price(db) -> None:
    from gdx_dispatch.models.pricing_engine import seed_default_pricing
    from gdx_dispatch.routers.parts_needed import _resolved_unit_price

    seed_default_pricing(db)
    db.commit()

    job = _job(db)
    cat = _catalog(db)
    # The QuickBooks-import shape: price column filled with the cost.
    _catalog_item(db, cat, sku="CHI-2294", price=2207.00, cost=2207.00,
                  category="doors", name="CHI 2294 10x8")

    class _Payload:
        sku = "CHI-2294"
        unit_price = 2207.00  # what the picker posts today

    stored, source = _resolved_unit_price(db, str(job.id), _Payload())
    assert stored is not None
    assert Decimal(str(stored)) > Decimal("2207.00"), (
        f"stored {stored} — the office would bill a door at cost"
    )
    # Migration 075: the row now says the markup engine produced this, NOT the
    # office — which is the whole point. A reader deciding whether to trust
    # $2,207-plus-margin has to know a machine chose it.
    assert source == "catalog_cost", source


def test_a_hand_typed_price_for_an_unknown_part_is_still_honoured(db) -> None:
    """Re-deriving must not break manual entry."""
    from gdx_dispatch.routers.parts_needed import _resolved_unit_price

    job = _job(db)

    class _Payload:
        sku = None
        unit_price = 42.00

    stored, source = _resolved_unit_price(db, str(job.id), _Payload())
    assert stored == 42.00
    # And it is recorded as a human's decision, not a machine's — the most
    # authoritative provenance in the table (migration 075).
    assert source == "office", source
