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


def test_manual_strategy_never_stores_cost_as_the_retail_price(db_session):
    """Regression: `manual` used to store our COST as the customer price.

    `manual` is the DEFAULT strategy and `compute_price` returns None for it, so
    the old `float(price if price is not None else (cost or 0))` fallback was the
    operative branch — a cost-only import wrote a zero-margin sell price into the
    catalog the estimate pickers read from. The previous version of this test
    asserted exactly that (`price == cost == 50.0`, commented "falls back to
    cost"), which is how the defect survived.

    With no margin tiers seeded here the engine cannot price it either, so the
    honest answer is None — which also trips the tenant's zero-price policy and
    puts it in front of a human.
    """
    cat = _make_catalog(db_session, "manual")
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P2", name="Bracket", cost=50.0),
        _mock_request(), _user(), db_session,
    )
    assert item["price"] != pytest.approx(50.0), "stored our cost as the retail price"
    assert item["price"] in (None, 0) or item["price"] > 50.0


def test_manual_strategy_falls_back_to_the_margin_engine(db_session):
    """With tiers configured — production's actual state — `manual` marks the
    cost up through them instead of leaving the item unpriced."""
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db_session)
    db_session.commit()
    cat = _make_catalog(db_session, "manual")
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P2M", name="Bracket", cost=50.0),
        _mock_request(), _user(), db_session,
    )
    assert item["price"] is not None
    assert item["price"] > 50.0, "a margin on cost, never the cost itself"


def test_read_time_price_comes_from_the_engine_not_the_stored_column(db_session):
    """CONTRACT CHANGE, recorded deliberately.

    An entered price is still STORED (`price_stored`), but the price served to
    callers is derived at read time — catalog strategy, then margin tiers.

    Why: every stored price in this tenant's catalogs is already engine output
    (the price/cost ratios are exactly the configured margins), so the column is
    a CACHE, not an independent sell price. Honouring it meant a margin change
    silently did nothing. Reading through the engine makes a margin change take
    effect everywhere at once.

    The cost: a genuinely hand-entered override is no longer honoured at read
    time. Nothing distinguishes "a human typed this" from "the engine wrote it
    six months ago" — both are just `price`. Restoring overrides needs a
    provenance flag on the row, which is a migration and its own change.
    """
    cat = _make_catalog(db_session, "keystone")
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="P3", name="Priced", cost=100.0, price=175.0),
        _mock_request(), _user(), db_session,
    )
    assert item["price"] == pytest.approx(200.0), "keystone on cost, not the entered 175"
    assert item["price_stored"] == pytest.approx(175.0), "the entered value is still kept"


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


# ── #54: QB pull and AI import apply the strategy too ───────────────────────

def test_qb_pull_applies_strategy(db_session):
    # QB sync pull priced cost-only rows at retail=cost; now it routes through
    # the catalog strategy like the form/CSV paths.
    cat = _make_catalog(db_session, "keystone")
    catalog_obj = catalog_router._get_catalog_or_404(UUID(cat["id"]), db_session)
    action = catalog_router._upsert_qb_item(
        catalog_obj, {"sku": "QB1", "name": "QB Widget", "cost": 100}, db_session,
    )
    db_session.commit()
    assert action == "created"
    listing = catalog_router.list_catalog_items(
        UUID(cat["id"]), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )
    assert listing["items"][0]["price"] == pytest.approx(200.0)  # not 100


class _FakeUpload:
    """Minimal UploadFile stand-in: async .read() yielding the given bytes."""

    def __init__(self, data: bytes):
        self._data = data
        self.filename = "sheet.txt"

    async def read(self) -> bytes:
        return self._data


def test_ai_import_applies_strategy(db_session, monkeypatch):
    import asyncio

    import gdx_dispatch.core.ai_router as ai_router

    cat = _make_catalog(db_session, "keystone")

    class _FakeRouter:
        async def generate(self, **_kw):
            return '[{"sku": "AI1", "name": "AI Widget", "cost": 100}]'

    monkeypatch.setattr(ai_router, "get_ai_router", lambda: _FakeRouter())
    asyncio.run(catalog_router.ai_import_catalog(
        UUID(cat["id"]), _mock_request(),
        file=_FakeUpload(b"AI Widget 100"), user=_user(), db=db_session,
    ))
    listing = catalog_router.list_catalog_items(
        UUID(cat["id"]), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )
    assert listing["items"][0]["price"] == pytest.approx(200.0)  # not 100


# ── #52: AI import chunking / PDF / partial handling ────────────────────────

def test_chunk_text_splits_on_line_boundaries():
    text = "".join(f"line{i} value\n" for i in range(2000))
    chunks = catalog_router._chunk_text_for_ai(text, max_chars=500)
    assert len(chunks) > 1
    assert "".join(chunks) == text          # lossless
    assert all(len(c) <= 500 + 20 for c in chunks)  # ~bounded (one line of slack)


def test_parse_ai_json_array_tolerates_fences_and_prose():
    p = catalog_router._parse_ai_json_array
    assert p('```json\n[{"name":"A"}]\n```') == [{"name": "A"}]
    assert p('Here are the parts:\n[{"name":"B"}]\nDone.') == [{"name": "B"}]
    import pytest as _pytest
    with _pytest.raises(Exception):
        p('{"name":"not a list"}')


def test_ai_import_paginates_large_sheet(db_session, monkeypatch):
    # A sheet large enough to span multiple chunks → one model call per chunk,
    # items accumulated across calls (no truncation).
    import asyncio

    import gdx_dispatch.core.ai_router as ai_router
    cat = _make_catalog(db_session, "keystone")
    big_text = "".join(f"PART-{i}, Widget {i}, 10\n" for i in range(3000))  # > 12KB → multi-chunk

    calls = {"n": 0}

    class _Router:
        async def generate(self, **_kw):
            calls["n"] += 1
            return f'[{{"sku":"AI-{calls["n"]}","name":"Chunk {calls["n"]} Item","cost":100}}]'

    monkeypatch.setattr(ai_router, "get_ai_router", lambda: _Router())

    class _Up:
        filename = "sheet.txt"
        async def read(self):
            return big_text.encode()

    res = asyncio.run(catalog_router.ai_import_catalog(
        UUID(cat["id"]), _mock_request(), file=_Up(), user=_user(), db=db_session,
    ))
    assert calls["n"] > 1                 # paginated
    assert res["chunks"] == calls["n"]
    assert res["imported"] == calls["n"]  # one item per chunk accumulated
    assert res["partial"] is False


def test_ai_import_partial_when_a_chunk_fails(db_session, monkeypatch):
    import asyncio

    import gdx_dispatch.core.ai_router as ai_router
    cat = _make_catalog(db_session, "manual")
    big_text = "".join(f"PART-{i}, Widget {i}, 10\n" for i in range(3000))

    state = {"n": 0}

    class _Router:
        async def generate(self, **_kw):
            state["n"] += 1
            if state["n"] == 1:
                return "this is not json at all"   # first chunk fails to parse
            return '[{"sku":"OK","name":"Good","cost":5}]'

    monkeypatch.setattr(ai_router, "get_ai_router", lambda: _Router())

    class _Up:
        filename = "sheet.txt"
        async def read(self):
            return big_text.encode()

    res = asyncio.run(catalog_router.ai_import_catalog(
        UUID(cat["id"]), _mock_request(), file=_Up(), user=_user(), db=db_session,
    ))
    assert res["failed_chunks"] >= 1
    assert res["partial"] is True
    assert res["imported"] >= 1            # surviving chunks still imported


def test_extract_import_text_reads_pdf(db_session):
    # Server-side PDF extraction via pypdf (#52).
    pytest.importorskip("pypdf")
    from io import BytesIO

    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = BytesIO()
    w.write(buf)
    pdf_bytes = buf.getvalue()
    assert pdf_bytes[:5] == b"%PDF-"
    # Blank page extracts to empty/near-empty text but must not raise.
    out = catalog_router._extract_import_text(pdf_bytes, "sheet.pdf")
    assert isinstance(out, str)


def test_parse_ai_json_array_reads_router_content_key():
    # AIRouter.generate() returns {"content": "..."} — the parser MUST read
    # that key (reading "text" returned "" → 502 on every real upload).
    p = catalog_router._parse_ai_json_array
    assert p({"content": '[{"name":"A","cost":1}]', "model": "x"}) == [{"name": "A", "cost": 1}]
    # 'text' kept as a tolerant fallback for direct-string callers.
    assert p({"text": '[{"name":"B"}]'}) == [{"name": "B"}]


def test_ai_import_uses_router_dict_response(db_session, monkeypatch):
    # End-to-end with the REAL generate() return shape (a dict with "content"),
    # not a bare string — the shape production actually produces.
    import asyncio

    import gdx_dispatch.core.ai_router as ai_router
    cat = _make_catalog(db_session, "keystone")

    class _Router:
        async def generate(self, **_kw):
            return {"content": '[{"sku":"R1","name":"Real Widget","cost":100}]',
                    "model": "test", "tokens_used": 1}

    monkeypatch.setattr(ai_router, "get_ai_router", lambda: _Router())

    class _Up:
        filename = "sheet.txt"
        async def read(self):
            return b"Real Widget 100"

    res = asyncio.run(catalog_router.ai_import_catalog(
        UUID(cat["id"]), _mock_request(), file=_Up(), user=_user(), db=db_session,
    ))
    assert res["imported"] == 1
    assert res["failed_chunks"] == 0
    listing = catalog_router.list_catalog_items(
        UUID(cat["id"]), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )
    assert listing["items"][0]["price"] == pytest.approx(200.0)  # strategy applied


# ── Drift guards ────────────────────────────────────────────────────────────
# The defect this file keeps re-learning: a customer-facing price that is a
# stale copy of our cost, or a stale copy of an old engine answer. These tests
# exist to make that impossible to reintroduce quietly.


def test_a_margin_change_moves_every_catalog_price(db_session):
    """THE drift guard.

    Stored prices in production are engine output — the price/cost ratios are
    exactly the configured margins. If the read path ever goes back to serving
    the stored column, this fails: change the margin, and the price served must
    change with it. That is the whole point of pricing at read time.
    """
    from gdx_dispatch.models.pricing_engine import MarginTier, PricingTierSet, seed_default_pricing

    seed_default_pricing(db_session)
    db_session.commit()

    cat = _make_catalog(db_session, "manual")
    item = catalog_router.add_catalog_item(
        UUID(cat["id"]),
        CatalogItemCreateIn(sku="DRIFT-1", name="Bracket", cost=100.0),
        _mock_request(), _user(), db_session,
    )
    first = item["price"]
    assert first is not None and first > 100.0

    # Move every retail parts margin.
    tier_set = (
        db_session.query(PricingTierSet)
        .filter(PricingTierSet.pricing_category == "parts",
                PricingTierSet.pricing_class == "retail")
        .first()
    )
    assert tier_set is not None, "fixture must seed a parts/retail tier set"
    for tier in db_session.query(MarginTier).filter(MarginTier.tier_set_id == tier_set.id):
        tier.margin_pct = Decimal("0.10")
    db_session.commit()

    again = catalog_router.list_catalog_items(
        UUID(cat["id"]), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )["items"][0]

    assert again["price"] != pytest.approx(first), (
        "the margin changed but the served price did not — the read path is "
        "serving a stale cached price again"
    )
    assert again["price"] == pytest.approx(100.0 / (1 - 0.10), rel=1e-3)


def test_the_served_price_is_never_our_cost(db_session):
    """No catalog row may ever offer the customer our cost, or $0.

    Covers both shapes seen in production: `price == cost` (QuickBooks-style
    imports where the price column was filled with the cost) and `price = 0`
    (a cache that was never computed — two live Springs rows sat at 0.00
    against a cost of 78.00).
    """
    from gdx_dispatch.models.pricing_engine import seed_default_pricing

    seed_default_pricing(db_session)
    db_session.commit()
    cat = _make_catalog(db_session, "manual")

    for sku, cost, entered in (
        ("POISON-EQ", 78.0, 78.0),   # price == cost
        ("POISON-ZERO", 78.0, 0.0),  # price == 0
        ("POISON-LOW", 78.0, 10.0),  # price < cost
    ):
        catalog_router.add_catalog_item(
            UUID(cat["id"]),
            CatalogItemCreateIn(sku=sku, name=sku, cost=cost, price=entered),
            _mock_request(), _user(), db_session,
        )

    served = catalog_router.list_catalog_items(
        UUID(cat["id"]), search=None, page=1, per_page=25, _=_user(), db=db_session,
    )["items"]
    assert served, "expected the seeded items back"
    for row in served:
        price, cost = row["price"], row["cost"]
        assert price is None or price > cost, (
            f"{row['sku']} offers {price} against a cost of {cost} — "
            "that is our cost or worse, on a customer-facing quote"
        )
        assert price is None or price > 0, f"{row['sku']} offers a $0 line"
