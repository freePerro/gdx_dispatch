"""sku-suggest contract — it must be able to suggest a *part*.

Two contracts live here:

1. **part_id only on ``source='parts'``** (Phase 2 / C5, Doug 2026-05-10).
   MobileJobCloseoutDialog reads ``s.part_id`` for inventory rows and uses it
   to write a JobPart row (inventory ledger). Every other source must leave it
   out: ``job_parts.part_id`` is an FK to ``parts.id``, so a catalog id there
   is an FK violation (the C2 hotfix bug).

2. **The tech can find a part at all** (2026-07-16). This endpoint shipped
   unable to suggest one. Its custom-catalog source was filtered to
   ``product_class == 'door'`` — a value matching **zero** production rows —
   and ``chi_parts_catalog`` was never queried. With ``parts`` empty, the only
   source that could return anything was the door catalog, so searching
   "spring" returned 8 doors whose marketing copy contains the word while all
   90 real springs stayed invisible.

These were source-text regex pins until 2026-07-16. They could not see bug 2 —
the doors-only filter was plainly visible in the very text they matched — and
the door-leak pin was wrapped in ``if door_branch:``, so it passed vacuously
the moment the call was renamed. Both now run the real query against real rows.
"""
from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.models.tenant_models import (
    ChiDoorCatalog,
    ChiPartsCatalog,
    CustomCatalogItem,
    DoorSpec,
)
from gdx_dispatch.modules.inventory.models import Part
from gdx_dispatch.routers import parts_needed

# A CHI door description is a marketing paragraph, not a name. It mentions
# "spring" in passing, which is exactly how doors hijacked a parts search.
_DOOR_BLURB = (
    "Timeless craftsmanship meets modern performance in this insulated "
    "carriage-house door. Powder-coated hardware and a spring-assisted "
    "counterbalance deliver quiet, reliable operation for years to come. "
) * 4


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (Part, CustomCatalogItem, ChiPartsCatalog, ChiDoorCatalog, DoorSpec):
        model.__table__.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    return req


def _suggest(db: Session, q: str, limit: int = 10) -> list[dict]:
    return parts_needed.sku_suggest(
        request=_request(), q=q, limit=limit, user={"tenant_id": "tenant-a"}, db=db
    )


def _add_custom_part(db: Session, *, sku: str, name: str, product_class: str = "parts") -> None:
    db.add(
        CustomCatalogItem(
            id=uuid.uuid4(),
            catalog_id=uuid.uuid4(),
            sku=sku,
            name=name,
            product_class=product_class,
            active=True,
        )
    )
    db.commit()


def _add_chi_door(db: Session, *, sku: str, model_number: str) -> None:
    db.add(
        ChiDoorCatalog(
            id=uuid.uuid4(),
            sku=sku,
            model_number=model_number,
            description=_DOOR_BLURB,
            is_active=True,
        )
    )
    db.commit()


# ── Contract 2: a tech can find a part ───────────────────────────────────────


def test_finds_a_custom_catalog_part(db: Session) -> None:
    """The production bug, reproduced.

    2,561 of the tenant's catalog rows are product_class='parts' and 0 are
    'door'. The old door-only filter made every one of them unsearchable.
    """
    _add_custom_part(db, sku="SPR-207", name="Torsion spring .207 x 2 x 27")

    hits = _suggest(db, "spring")

    assert [h["sku"] for h in hits] == ["SPR-207"], (
        "the tenant's own catalog part is invisible to the tech's search"
    )


def test_finds_a_chi_parts_catalog_part(db: Session) -> None:
    """chi_parts_catalog was never queried — it holds 78 of the 90 springs."""
    db.add(
        ChiPartsCatalog(
            id=uuid.uuid4(),
            sku="CHI-SPR-01",
            name="Torsion spring, 2in ID",
            part_type="spring",
            is_active=True,
        )
    )
    db.commit()

    hits = _suggest(db, "spring")

    assert [h["sku"] for h in hits] == ["CHI-SPR-01"], "CHI parts are not searched"


def test_parts_outrank_doors(db: Session) -> None:
    """Doors must not eat the limit.

    Their descriptions match nearly any needle, so with doors ranked ahead of
    parts a full page of them buried the real result.
    """
    _add_custom_part(db, sku="SPR-207", name="Torsion spring .207 x 2 x 27")
    for i in range(9):
        _add_chi_door(db, sku=f"DOOR-{i}", model_number=f"CH-{i}")

    hits = _suggest(db, "spring", limit=3)

    assert hits[0]["sku"] == "SPR-207", (
        f"a door outranked the spring: {[h['sku'] for h in hits]}"
    )


def test_a_door_is_named_by_model_number_not_its_marketing_blurb(db: Session) -> None:
    """pickSuggestion() assigns `name` straight to the part name it saves, so
    what we pick here lands in the job record. description-first is how a part
    came to be named three sentences about curb appeal."""
    _add_chi_door(db, sku="DOOR-1", model_number="CH-2250")

    hit = _suggest(db, "carriage")[0]

    assert hit["name"] == "CH-2250", f"named by blurb, not model: {hit['name'][:60]!r}"


def test_a_long_name_is_trimmed(db: Session) -> None:
    """The trim path only runs when the first non-empty candidate is itself
    long — a door with no model_number falls through to the ~856-char blurb.
    (The model-number test above never reaches this branch, which is why it
    lives separately.)"""
    _add_chi_door(db, sku="DOOR-2", model_number=None)

    hit = _suggest(db, "carriage")[0]

    assert len(hit["name"]) <= 120, f"{len(hit['name'])}-char part name would be saved"
    assert hit["name"].endswith("..."), "a trimmed name should show it was trimmed"


def test_a_sku_in_two_catalogs_is_suggested_once(db: Session) -> None:
    _add_custom_part(db, sku="SHARED-1", name="Roller, 2in nylon")
    db.add(
        ChiPartsCatalog(
            id=uuid.uuid4(), sku="SHARED-1", name="Roller, 2in nylon", is_active=True
        )
    )
    db.commit()

    hits = _suggest(db, "roller")

    assert len(hits) == 1, f"duplicate sku offered twice: {hits}"


# ── Contract 1: part_id rides only on inventory rows ─────────────────────────


def test_part_id_present_for_inventory_rows(db: Session) -> None:
    part = Part(id=uuid.uuid4(), sku="INV-1", name="Torsion spring", qty_on_hand=4)
    db.add(part)
    db.commit()

    hit = _suggest(db, "spring")[0]

    assert hit["source"] == "parts"
    assert hit["part_id"] == str(part.id), (
        "without part_id the closeout degrades to snapshot-only and inventory "
        "math never updates"
    )
    assert hit["qty_on_hand"] == 4


def test_no_other_source_leaks_a_part_id(db: Session) -> None:
    """Unconditional — the old pin skipped itself when its regex missed.

    job_parts.part_id is an FK to parts.id; an id from any other table
    FK-violates on closeout.
    """
    _add_custom_part(db, sku="SPR-207", name="Torsion spring")
    _add_custom_part(db, sku="CD-1", name="Custom carriage door", product_class="door")
    _add_chi_door(db, sku="DOOR-1", model_number="CH-2250")
    db.add(
        ChiPartsCatalog(id=uuid.uuid4(), sku="CHI-1", name="Spring, torsion", is_active=True)
    )
    db.commit()

    hits = _suggest(db, "spring", limit=50)
    non_inventory = [h for h in hits if h["source"] != "parts"]

    assert non_inventory, "fixture produced no non-inventory rows to check"
    for hit in non_inventory:
        assert hit.get("part_id") is None, (
            f"{hit['source']} suggestion {hit['sku']} carries part_id — closeout "
            f"would FK-violate against parts.id"
        )


def test_soft_deleted_and_inactive_rows_stay_hidden(db: Session) -> None:
    from datetime import datetime, timezone

    db.add(
        CustomCatalogItem(
            id=uuid.uuid4(),
            catalog_id=uuid.uuid4(),
            sku="GONE-1",
            name="Retired spring",
            product_class="parts",
            active=True,
            deleted_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        CustomCatalogItem(
            id=uuid.uuid4(),
            catalog_id=uuid.uuid4(),
            sku="OFF-1",
            name="Inactive spring",
            product_class="parts",
            active=False,
        )
    )
    db.commit()

    assert _suggest(db, "spring") == []


def test_empty_query_returns_nothing(db: Session) -> None:
    _add_custom_part(db, sku="SPR-207", name="Torsion spring")
    assert _suggest(db, "   ") == []


def test_endpoint_is_permission_gated() -> None:
    """The gate must be on THIS route, not merely somewhere in the module.

    Asserting `'require_permission("inventory.read")' in src` is what this was,
    and it was worthless: the string appears 4 times in the file, so deleting
    sku_suggest's own gate left the test green (verified by mutation). That is
    the same vacuous-pin failure this file's header condemns — written straight
    back into it. Introspect the real route's dependencies instead.
    """
    # The router carries prefix="/api", so the decorator's literal path is not
    # the route's path.
    route = next(
        r for r in parts_needed.router.routes
        if getattr(r, "path", None) == "/api/parts-needed/sku-suggest"
    )
    required: set[str] = set()
    for dep in route.dependant.dependencies:
        for cell in getattr(dep.call, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:  # pragma: no cover — empty cell
                continue
            if isinstance(value, set) and all(isinstance(v, str) for v in value):
                required |= value

    assert "inventory.read" in required, (
        f"sku-suggest lost its inventory.read gate; route requires {required or '{}'}"
    )
