"""Customer tags have a read surface: a list filter, a list column, a segment rule (GDXA-1046).

After the bulk-tag fix (#462 / #570) tags were really written, and nothing could
use them at scale: GET /api/customers returned no tags and took no tag filter,
and the segment rollup carried no tag field, so no segment could select by tag.
The only reader was one customer at a time. These tests run the real routes
against an ORM-built SQLite schema, where `customers.id` is 32 dashless hex and
`tag_assignments.entity_id` is dashed text, the split a raw comparison misses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.models.tenant_models import Customer, Tag, TagAssignment, TenantBase
from gdx_dispatch.routers import customers as customers_mod
from gdx_dispatch.routers.customers import list_customers
from gdx_dispatch.routers.segments import (
    SegmentCreateIn,
    SegmentPatch,
    create_segment,
    list_segment_customers,
    list_segments,
    update_segment,
)
from gdx_dispatch.routers.tags import (
    AssignTagIn,
    assign_tag_to_customer,
    delete_tag,
    list_customer_tags,
    unassign_tag_from_customer,
)

pytestmark = pytest.mark.anyio

TENANT = "tenant-test"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    # The unfiltered list is cached per process; every test here reads its own
    # database, so the cache must not hand one test another's rows.
    async def _no_cache(tenant_id, key, ttl_seconds, fetcher):
        return fetcher()

    monkeypatch.setattr(customers_mod, "cached", _no_cache)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request():
    return SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}), client=None, headers={})


def _user():
    return {"sub": "user-1", "user_id": "user-1"}


def _customer(db, name, *, deleted=False):
    c = Customer(
        id=uuid.uuid4(), company_id=TENANT, name=name, created_at=datetime.now(UTC),
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(c)
    db.commit()
    return str(c.id)


def _tag(db, name, *, color="#123456", deleted=False):
    t = Tag(company_id=TENANT, name=name, color=color,
            deleted_at=datetime.now(UTC) if deleted else None)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _assign(db, tag, customer_id):
    # Stored as given: tests pass the canonical id, or a legacy spelling to
    # stand in for a row written before the routes canonicalised (GDXA-1046).
    db.add(TagAssignment(company_id=TENANT, tag_id=tag.id, entity_type="customer",
                         entity_id=customer_id))
    db.commit()


async def _list(db, **kw):
    return await list_customers(request=_request(), q=kw.get("q"), page=1, per_page=50,
                                tag_id=kw.get("tag_id"), _={}, db=db)


# ── GET /api/customers ─────────────────────────────────────────────────────


async def test_tag_filter_returns_only_the_tagged_live_customers(db):
    vip = _tag(db, "VIP")
    a, b = _customer(db, "Alpha"), _customer(db, "Bravo")
    _customer(db, "Charlie")
    gone = _customer(db, "Delta", deleted=True)
    for cid in (a, b, gone):
        _assign(db, vip, cid)

    out = await _list(db, tag_id=str(vip.id))

    assert {c.id for c in out.items} == {a, b}
    assert out.total == 2, "total must count the filtered set, not the table"


async def test_tag_filter_and_search_narrow_together(db):
    vip = _tag(db, "VIP")
    a, b = _customer(db, "Alpha"), _customer(db, "Bravo")
    _assign(db, vip, a)
    _assign(db, vip, b)

    out = await _list(db, tag_id=str(vip.id), q="brav")

    assert [c.id for c in out.items] == [b]


async def test_tag_filter_refuses_an_unknown_deleted_or_malformed_tag(db):
    dead = _tag(db, "Old", deleted=True)
    for bad in (str(uuid.uuid4()), str(dead.id), "not-a-uuid"):
        with pytest.raises(HTTPException) as exc:
            await _list(db, tag_id=bad)
        assert exc.value.status_code == 422, bad


async def test_tag_filter_is_never_served_from_the_cache(db, monkeypatch):
    """A tag assignment does not invalidate the list cache, so a cached tagged
    list would miss the customer tagged a second ago."""
    calls: list[str] = []

    async def _spy(tenant_id, key, ttl_seconds, fetcher):
        calls.append(key)
        return fetcher()

    monkeypatch.setattr(customers_mod, "cached", _spy)
    vip = _tag(db, "VIP")
    _assign(db, vip, _customer(db, "Alpha"))

    await _list(db, tag_id=str(vip.id))
    assert calls == []


async def test_each_row_carries_its_live_tags_by_name(db):
    vip, net30, dead = _tag(db, "VIP", color="#aa0000"), _tag(db, "Net 30"), _tag(db, "Gone", deleted=True)
    a, plain = _customer(db, "Alpha"), _customer(db, "Plain")
    for t in (vip, net30, dead):
        _assign(db, t, a)

    out = await _list(db)
    rows = {c.id: c for c in out.items}

    assert [t.name for t in rows[a].tags] == ["Net 30", "VIP"], "deleted tag shown or order wrong"
    assert rows[a].tags[1].color == "#aa0000"
    assert rows[a].tags[1].id == str(vip.id)
    assert rows[plain].tags == [], "an untagged row must say no tags, not 'not loaded'"


async def test_row_tags_are_fresh_even_when_the_page_is_cached(db, monkeypatch):
    """The page may come from the cache; its tags must not."""
    a = _customer(db, "Alpha")
    first = await _list(db)
    stale = {"items": [i.model_dump(exclude={"tags"}) for i in first.items],
             "total": first.total, "page": 1, "per_page": 50}

    async def _stale_cache(tenant_id, key, ttl_seconds, fetcher):
        return stale

    monkeypatch.setattr(customers_mod, "cached", _stale_cache)
    _assign(db, _tag(db, "VIP"), a)

    out = await _list(db)
    assert [t.name for t in out.items[0].tags] == ["VIP"]


async def test_row_tags_cost_one_query_for_the_whole_page(db):
    vip = _tag(db, "VIP")
    for n in range(6):
        _assign(db, vip, _customer(db, f"C{n}"))

    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        if "tag_assignments" in statement:
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        out = await _list(db)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert len(out.items) == 6
    assert len(statements) == 1, f"N+1 on tags: {len(statements)} queries"


def _spellings(cid):
    return {"upper": cid.upper(), "dashless": cid.replace("-", "")}


@pytest.mark.parametrize("spelling", ["upper", "dashless"])
async def test_a_legacy_spelling_is_one_customer_to_every_reader(db, spelling):
    """A row stored before the routes canonicalised is still the same customer
    to the list filter, the row's tags, the segment rule and the detail tab."""
    vip = _tag(db, "VIP")
    a = _customer(db, "Alpha")
    _assign(db, vip, _spellings(a)[spelling])

    assert [i.id for i in (await _list(db, tag_id=str(vip.id))).items] == [a]
    assert [t.name for t in (await _list(db)).items[0].tags] == ["VIP"]
    seg = await _make_segment(db, {"field": "tag", "operator": "has", "value": str(vip.id)})
    rows = (await list_segment_customers(segment_id=seg.id, _={}, db=db)).items
    assert [str(uuid.UUID(str(r["id"]))) for r in rows] == [a]
    assert [t["name"] for t in list_customer_tags(a, _request(), {}, db)] == ["VIP"]

    # Re-assigning in the canonical spelling finds the legacy row, and
    # unassigning removes it: no second row, no 404.
    assign_tag_to_customer(a, AssignTagIn(tag_id=str(vip.id)), _request(), _user(), db)
    assert db.query(TagAssignment).count() == 1
    unassign_tag_from_customer(a, vip.id, _request(), _user(), db)
    assert db.query(TagAssignment).count() == 0


async def test_the_customer_route_stores_the_canonical_spelling(db):
    vip = _tag(db, "VIP")
    a = _customer(db, "Alpha")

    assign_tag_to_customer(a.upper(), AssignTagIn(tag_id=str(vip.id)), _request(), _user(), db)

    assert [r.entity_id for r in db.query(TagAssignment).all()] == [a]


async def test_one_tag_under_both_spellings_is_one_tag_and_untags_cleanly(db):
    """The unique constraint is on the raw entity_id, so a legacy row and a
    canonical row can both tie one tag to one customer. Assign must not 500 on
    two matches, every reader shows the chip once, and unassign clears both."""
    vip = _tag(db, "VIP")
    a = _customer(db, "Alpha")
    _assign(db, vip, a)
    _assign(db, vip, a.replace("-", "").upper())
    assert db.query(TagAssignment).count() == 2

    assert [t.name for t in (await _list(db)).items[0].tags] == ["VIP"]
    assert [i.id for i in (await _list(db, tag_id=str(vip.id))).items] == [a]
    assert [t["name"] for t in list_customer_tags(a, _request(), {}, db)] == ["VIP"]

    assign_tag_to_customer(a, AssignTagIn(tag_id=str(vip.id)), _request(), _user(), db)
    assert db.query(TagAssignment).count() == 2, "re-assign must reuse, not add"

    unassign_tag_from_customer(a, vip.id, _request(), _user(), db)
    assert db.query(TagAssignment).count() == 0, "a second spelling kept the tag on"
    assert (await _list(db)).items[0].tags == []


# ── Segments ───────────────────────────────────────────────────────────────


async def _make_segment(db, rules, name="Tagged"):
    return await create_segment(payload=SegmentCreateIn(name=name, rules=rules),
                                request=_request(), user=_user(), db=db)


async def test_a_segment_by_tag_selects_exactly_the_tagged_customers(db):
    vip, other = _tag(db, "VIP"), _tag(db, "Other")
    a, b = _customer(db, "Alpha"), _customer(db, "Bravo")
    c = _customer(db, "Charlie")
    gone = _customer(db, "Delta", deleted=True)
    _assign(db, vip, a)
    _assign(db, vip, b)
    _assign(db, vip, gone)
    _assign(db, other, c)

    has = await _make_segment(db, {"field": "tag", "operator": "has", "value": str(vip.id)})
    lacks = await _make_segment(
        db, {"field": "tag", "operator": "lacks", "value": str(vip.id)}, name="Not VIP",
    )

    has_rows = (await list_segment_customers(segment_id=has.id, _={}, db=db)).items
    lacks_rows = (await list_segment_customers(segment_id=lacks.id, _={}, db=db)).items

    def ids(rows):
        return {str(uuid.UUID(str(r["id"]))) for r in rows}

    assert ids(has_rows) == {a, b}
    assert ids(lacks_rows) == {c}
    # The table column reads the same rows the rule matched on.
    assert all([t["name"] for t in r["tags"]] == ["VIP"] for r in has_rows)

    counts = {s.id: s.matching_customer_count for s in (await list_segments(_={}, db=db)).items}
    assert counts[has.id] == 2
    assert counts[lacks.id] == 1

    # The list filter and the segment agree on who carries the tag.
    listed = await _list(db, tag_id=str(vip.id))
    assert {i.id for i in listed.items} == ids(has_rows)


async def test_a_tag_rule_combines_with_the_other_rules(db):
    vip = _tag(db, "VIP")
    a = _customer(db, "Alpha")
    _assign(db, vip, a)
    _customer(db, "Bravo")

    seg = await _make_segment(db, {"match": "all", "rules": [
        {"field": "tag", "operator": "has", "value": str(vip.id)},
        {"field": "created_at", "operator": "within_last", "value": "30 days"},
    ]})
    rows = (await list_segment_customers(segment_id=seg.id, _={}, db=db)).items
    assert [str(uuid.UUID(str(r["id"]))) for r in rows] == [a]


@pytest.mark.parametrize("how", ["soft", "hard"])
async def test_a_tag_deleted_after_the_segment_was_saved_matches_nobody(db, how):
    # The validator checks the tag on save only. Deleting it afterwards used
    # to turn `lacks` into every customer, and the export and bulk-tag read
    # that list. `soft` is DELETE /api/tags/{id}; `hard` is the admin delete.
    vip = _tag(db, "VIP")
    a = _customer(db, "Alpha")
    _customer(db, "Bravo")
    _assign(db, vip, a)
    has = await _make_segment(db, {"field": "tag", "operator": "has", "value": str(vip.id)})
    lacks = await _make_segment(
        db, {"field": "tag", "operator": "lacks", "value": str(vip.id)}, name="Not VIP",
    )
    assert len((await list_segment_customers(segment_id=lacks.id, _={}, db=db)).items) == 1

    if how == "soft":
        delete_tag(tag_id=vip.id, request=_request(), user=_user(), db=db)
    else:
        db.query(TagAssignment).filter(TagAssignment.tag_id == vip.id).delete()
        db.delete(vip)
        db.commit()

    for seg in (has, lacks):
        assert (await list_segment_customers(segment_id=seg.id, _={}, db=db)).items == []
    counts = {s.id: s.matching_customer_count for s in (await list_segments(_={}, db=db)).items}
    assert counts[has.id] == 0
    assert counts[lacks.id] == 0


async def test_metadata_cannot_stand_in_for_tags(db):
    vip = _tag(db, "VIP")
    a = _customer(db, "Alpha")
    row = db.get(Customer, uuid.UUID(a))
    row.metadata_ = {"tag_ids": [str(vip.id)], "tags": ["VIP"], "tag": str(vip.id)}
    db.commit()

    seg = await _make_segment(db, {"field": "tag", "operator": "has", "value": str(vip.id)})
    assert (await list_segment_customers(segment_id=seg.id, _={}, db=db)).items == []


@pytest.mark.parametrize(
    "rule, detail",
    [
        ({"field": "tag", "operator": "has", "value": "not-a-uuid"}, "Unknown tag"),
        ({"field": "tag", "operator": "has", "value": "UNKNOWN"}, "Unknown tag"),
        ({"field": "tag", "operator": "lacks", "value": "DELETED"}, "Unknown tag"),
        ({"field": "tag", "operator": "equals", "value": "VIP"}, "tag rule"),
        ({"field": "lifetime_value", "operator": "has", "value": "LIVE"}, "tag rule"),
        ({"field": "tag", "operator": "has", "value": ""}, "needs a value"),
    ],
)
async def test_the_validator_refuses_a_tag_rule_it_cannot_evaluate(db, rule, detail):
    live, dead = _tag(db, "VIP"), _tag(db, "Old", deleted=True)
    value = {"UNKNOWN": str(uuid.uuid4()), "DELETED": str(dead.id), "LIVE": str(live.id)}
    rule = {**rule, "value": value.get(rule["value"], rule["value"])}

    with pytest.raises(HTTPException) as exc:
        await _make_segment(db, rule)
    assert exc.value.status_code == 422
    assert detail in str(exc.value.detail)


async def test_the_validator_refuses_an_unknown_tag_on_edit_too(db):
    vip = _tag(db, "VIP")
    seg = await _make_segment(db, {"field": "tag", "operator": "has", "value": str(vip.id)})

    with pytest.raises(HTTPException) as exc:
        await update_segment(
            segment_id=seg.id,
            payload=SegmentPatch(rules={"field": "tag", "operator": "has", "value": str(uuid.uuid4())}),
            request=_request(), user=_user(), db=db,
        )
    assert exc.value.status_code == 422
