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
