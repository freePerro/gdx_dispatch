"""Integration tests for instant estimate — verifies real DB queries with seeded catalog data."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.routers.instant_estimate import InstantEstimateIn, instant_estimate


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-ie-test") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-ie1")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def ctx():
    engine = make_fresh_db()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SL()
    # make_fresh_db() already creates chi_door_catalog + chi_parts_catalog
    # via the real ORM metadata (32-col + 21-col tables respectively).
    # Previous fixture used positional VALUES INSERTs and hit NOT NULL
    # on columns with Python-side defaults (is_custom, is_active,
    # imported_at) because raw SQL skips the ORM default machinery.
    # Fix: use ORM objects so Python defaults fire. `id` is Uuid-typed
    # so we generate real UUIDs and export the values for the tests
    # to reference instead of the old 'door-1' / 'part-1' strings.
    import uuid

    from gdx_dispatch.models.pricing_engine import seed_default_pricing
    from gdx_dispatch.models.tenant_models import ChiDoorCatalog, ChiPartsCatalog

    # Production HAS margin tiers configured (15 active sets). Without them the
    # engine correctly refuses to price and every line is omitted, so a fixture
    # with no tiers would test a state the app is never in.
    seed_default_pricing(db)
    door1_id = uuid.uuid4()
    door2_id = uuid.uuid4()
    part1_id = uuid.uuid4()
    part2_id = uuid.uuid4()
    part3_id = uuid.uuid4()
    db.add_all([
        ChiDoorCatalog(
            id=door1_id, sku="SKU-DOOR-1", model_number="2283",
            description="Steel insulated", width=16, height=7,
            insulation_type="poly", section_material="steel",
            sell_price=None, cost=989.78,
        ),
        ChiDoorCatalog(
            id=door2_id, sku="SKU-DOOR-2", model_number="4216",
            description="Wood panel", width=9, height=8,
            insulation_type="none", section_material="wood",
            sell_price=1200.00, cost=800.00,
        ),
        ChiPartsCatalog(
            id=part1_id, sku="SKU-PART-1", name="Torsion Spring 207",
            part_type="Spring", sell_price=None, cost=45.00,
        ),
        ChiPartsCatalog(
            id=part2_id, sku="SKU-PART-2", name="LiftMaster 8550W Opener",
            part_type="Operators Residential", sell_price=None, cost=385.00,
        ),
        ChiPartsCatalog(
            id=part3_id, sku="SKU-PART-3", name="Steel Cable Set",
            part_type="Cable", sell_price=None, cost=32.00,
        ),
    ])
    db.commit()
    req = DummyRequest()
    user = {"user_id": "tech-1", "sub": "tech-1", "role": "tech"}
    try:
        yield db, req, user
    finally:
        db.close()
        engine.dispose()


def test_finds_door_by_dimensions(ctx):
    """Queries chi_door_catalog by width/height and prices it through the engine."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="16x7 steel door replacement"), user=user, db=db)
    assert result["suggested_door"] is not None
    assert result["suggested_door"]["model"] == "2283"
    # door-1 has sell_price NULL and cost 989.78 — the quoted number must be a
    # MARGIN on that cost, never the cost itself.
    assert result["suggested_door"]["price"] > 989.78


def test_never_quotes_a_door_at_our_cost(ctx):
    """Regression: this endpoint used to quote our DEALER COST to the customer.

    door-1 carries sell_price=NULL and cost=989.78, and the old
    ``COALESCE(sell_price, cost, 0)`` therefore emitted 989.78 as the customer
    price — a zero-margin sale. The previous version of this test asserted
    exactly that, which is how the defect survived. It must now be a margin on
    the cost, and must never equal it.
    """
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="16x7 steel replacement"), user=user, db=db)
    door_items = [i for i in result["line_items"] if i["source"] == "chi_catalog"]
    assert len(door_items) >= 1
    assert door_items[0]["unit_price"] != 989.78, "quoted our cost to the customer"
    assert door_items[0]["unit_price"] > 989.78


def test_finds_parts_by_keyword_spring(ctx):
    """'spring' keyword should find Torsion Spring from parts catalog."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="replace spring"), user=user, db=db)
    part_names = [i["name"] for i in result["line_items"] if i["source"] == "chi_parts"]
    assert any("Spring" in n for n in part_names)


def test_springs_returned_in_pairs(ctx):
    """Garage door springs are always replaced in pairs — qty must be 2."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="replace spring"), user=user, db=db)
    spring_items = [i for i in result["line_items"] if "spring" in i["name"].lower()]
    for s in spring_items:
        assert s["qty"] == 2, f"Spring '{s['name']}' should have qty=2 (pairs)"


def test_labor_rate_replacement(ctx):
    """Replacement labor must be $350."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="16x7 door replacement"), user=user, db=db)
    labor = [i for i in result["line_items"] if i["source"] == "labor"]
    assert len(labor) == 1
    assert labor[0]["unit_price"] == 350.0


def test_labor_rate_repair(ctx):
    """Repair labor must be $150."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="fix broken cable"), user=user, db=db)
    labor = [i for i in result["line_items"] if i["source"] == "labor"]
    assert labor[0]["unit_price"] == 150.0


def test_total_is_correct_sum(ctx):
    """Total must equal sum of qty * unit_price for all line items."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="16x7 steel replacement spring"), user=user, db=db)
    expected = sum(i["qty"] * i["unit_price"] for i in result["line_items"])
    assert abs(result["total"] - expected) < 0.01


def test_nonsensical_input_returns_labor_only(ctx):
    """Gibberish description should return only the labor item — no fake catalog matches."""
    db, req, user = ctx
    result = instant_estimate(request=req, payload=InstantEstimateIn(description="the purple elephant dances"), user=user, db=db)
    non_labor = [i for i in result["line_items"] if i["source"] != "labor"]
    assert len(non_labor) == 0, "Nonsensical input should not match any catalog items"


def test_never_quotes_a_part_at_our_cost(ctx):
    """The live half of the same defect: every CHI parts row has sell_price NULL,
    so ``sell_price or cost`` quoted our dealer cost on every part, every time."""
    db, req, user = ctx
    result = instant_estimate(
        request=req, payload=InstantEstimateIn(description="replace spring"), user=user, db=db
    )
    parts = [i for i in result["line_items"] if i["source"] == "chi_parts"]
    assert parts, "expected at least one part line"
    for p in parts:
        # Torsion Spring 207 costs 45.00 in the fixture.
        assert p["unit_price"] != 45.00, "quoted our cost to the customer"
        assert p["unit_price"] > 45.00


def test_unpriceable_part_is_omitted_not_zero(ctx):
    """A part the engine cannot price must be LEFT OUT.

    The two wrong answers are $0 (a free part on a customer quote) and cost (a
    zero-margin sale). An absent line is the honest one — it prompts the office
    to price it.
    """
    import uuid as _uuid

    from gdx_dispatch.models.tenant_models import ChiPartsCatalog

    # No cost and no sell price — nothing the engine can work from.
    db, req, user = ctx
    db.add(ChiPartsCatalog(
        id=_uuid.uuid4(), sku="SKU-UNPRICEABLE", name="Mystery Bracket",
        part_type="Bracket", sell_price=None, cost=None,
    ))
    db.commit()

    result = instant_estimate(
        request=req, payload=InstantEstimateIn(description="need a bracket"), user=user, db=db
    )
    names = [i["name"] for i in result["line_items"]]
    assert "Mystery Bracket" not in names
    assert all(i["unit_price"] > 0 for i in result["line_items"]), "a $0 line reached the quote"


def test_the_estimate_is_actually_audited(ctx):
    """Counterfactual: drop the db.commit() and this fails.

    log_audit_event_sync only add()s + flush()es and get_db never commits, so
    the audit row used to die with the request — the endpoint had NO trail, and
    its zero audit rows read as "never used" when they meant "never recorded".
    """
    from gdx_dispatch.core.audit import AuditLog

    db, req, user = ctx
    before = db.query(AuditLog).filter(AuditLog.entity_type == "instant_estimate").count()
    instant_estimate(
        request=req, payload=InstantEstimateIn(description="16x7 steel replacement"),
        user=user, db=db,
    )
    # ROLLBACK is the whole test. log_audit_event_sync add()s + flush()es, and a
    # flushed row is visible to its own session whether or not it was ever
    # committed — so simply counting here passes even with the commit removed
    # (verified: it did). Only a rollback tells the two apart: a committed row
    # survives it, a merely-flushed one is discarded with the transaction.
    db.rollback()
    after = db.query(AuditLog).filter(AuditLog.entity_type == "instant_estimate").count()
    assert after == before + 1, "the audit row did not survive a rollback — it was never committed"
