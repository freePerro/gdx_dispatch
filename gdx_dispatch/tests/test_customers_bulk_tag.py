"""`POST /api/customers/bulk-tag` writes tags, or says it did not (#462 / #570).

The handler this replaces took no `db` at all and returned
`{"ok": True, "tagged": len(customer_ids)}`. The Segments toolbar calls it and
toasted "Tag applied to selected customers", so office staff were told a tag
landed on every selected customer and none ever did — CLAUDE.md's highest
defect class, a success response without the work.

A second, honest copy in ui_compat raised 501 and was never reachable:
sub_resources is included first. It is gone now; these tests pin the survivor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import ensure_audit_table
from gdx_dispatch.models.tenant_models import Customer, Tag, TagAssignment
from gdx_dispatch.routers.sub_resources import BulkTagIn, customers_bulk_tag

TENANT = "test-tenant"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    for model in (Customer, Tag, TagAssignment):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    monkeypatch.setattr("gdx_dispatch.routers.sub_resources.company_id", lambda: TENANT)
    monkeypatch.setattr("gdx_dispatch.routers.tags.company_id", lambda: TENANT, raising=False)
    yield session
    session.close()


def _request():
    r = MagicMock()
    r.state.tenant = {"id": TENANT}
    r.client.host = "127.0.0.1"
    return r


def _user():
    return {"sub": "user-1", "user_id": "user-1"}


def _customer(db, name="Acme", deleted=False):
    c = Customer(id=uuid.uuid4(), company_id=TENANT, name=name,
                 created_at=datetime.now(UTC),
                 deleted_at=datetime.now(UTC) if deleted else None)
    db.add(c)
    db.commit()
    return str(c.id)


def _assignments(db):
    return db.execute(select(TagAssignment)).scalars().all()


def _audit(db, action):
    return db.execute(
        text("SELECT entity_id, user_id FROM audit_logs WHERE action = :a"), {"a": action}
    ).fetchall()


def _call(db, ids, tag):
    return customers_bulk_tag(payload=BulkTagIn(customer_ids=ids, tag=tag),
                              request=_request(), user=_user(), db=db)


# ── it actually writes ─────────────────────────────────────────────────────


def test_bulk_tag_creates_the_tag_and_assigns_it_to_every_customer(db):
    ids = [_customer(db, "A"), _customer(db, "B"), _customer(db, "C")]

    out = _call(db, ids, "VIP")

    assert out["tagged"] == 3
    assert out["tag"]["created"] is True
    assert out["not_found"] == []

    tag = db.execute(select(Tag).where(Tag.name == "VIP")).scalar_one()
    assigned = {a.entity_id for a in _assignments(db)}
    assert assigned == set(ids), "not every selected customer was tagged"
    assert all(a.tag_id == tag.id and a.entity_type == "customer" for a in _assignments(db))


def test_every_assignment_writes_an_audit_row_naming_the_user(db):
    """Invariant #1. The stub wrote none, because it wrote nothing."""
    ids = [_customer(db, "A"), _customer(db, "B")]

    _call(db, ids, "VIP")

    rows = _audit(db, "tag_assigned")
    assert len(rows) == 2, f"expected one audit row per assignment, got {rows}"
    assert {r[1] for r in rows} == {"user-1"}
    assert _audit(db, "tag_created"), "creating the tag must be audited too"


def test_reuses_an_existing_tag_rather_than_duplicating_it(db):
    existing = Tag(company_id=TENANT, name="VIP")
    db.add(existing)
    db.commit()
    db.refresh(existing)

    out = _call(db, [_customer(db, "A")], "VIP")

    assert out["tag"]["created"] is False
    assert out["tag"]["id"] == str(existing.id)
    assert len(db.execute(select(Tag)).scalars().all()) == 1


def test_resurrects_a_soft_deleted_tag_of_the_same_name(db):
    """Matches POST /api/tags, which resurrects rather than 409ing forever."""
    dead = Tag(company_id=TENANT, name="VIP", deleted_at=datetime.now(UTC))
    db.add(dead)
    db.commit()

    out = _call(db, [_customer(db, "A")], "VIP")

    assert out["tag"]["created"] is True
    assert db.execute(select(Tag).where(Tag.name == "VIP")).scalar_one().deleted_at is None


def test_retagging_is_idempotent(db):
    cid = _customer(db, "A")
    _call(db, [cid], "VIP")
    out = _call(db, [cid], "VIP")

    assert out["tagged"] == 1
    assert len(_assignments(db)) == 1, "re-tagging duplicated the assignment"


def test_duplicate_ids_in_one_request_are_collapsed(db):
    cid = _customer(db, "A")
    out = _call(db, [cid, cid, cid], "VIP")

    assert out["requested"] == 1
    assert out["tagged"] == 1
    assert len(_assignments(db)) == 1


# ── it refuses to pretend ──────────────────────────────────────────────────


def test_unknown_customer_ids_are_reported_not_silently_counted(db):
    """The old handler returned `tagged: len(ids)` for ids that did not exist."""
    real = _customer(db, "A")
    ghost = str(uuid.uuid4())

    out = _call(db, [real, ghost], "VIP")

    assert out["tagged"] == 1, "a non-existent customer must not count as tagged"
    assert out["not_found"] == [ghost]
    assert {a.entity_id for a in _assignments(db)} == {real}


def test_soft_deleted_customers_are_not_tagged(db):
    live = _customer(db, "A")
    gone = _customer(db, "B", deleted=True)

    out = _call(db, [live, gone], "VIP")

    assert out["tagged"] == 1
    assert out["not_found"] == [gone]


def test_blank_tag_is_refused_by_validation(db):
    with pytest.raises(Exception):
        BulkTagIn(customer_ids=[str(uuid.uuid4())], tag="")


def test_empty_selection_is_refused_by_validation(db):
    with pytest.raises(Exception):
        BulkTagIn(customer_ids=[], tag="VIP")


def test_whitespace_only_tag_writes_nothing(db):
    cid = _customer(db, "A")
    with pytest.raises(Exception):
        _call(db, [cid], "   ")
    assert _assignments(db) == []


# ── through the ROUTE, not just the function ──────────────────────────────
#
# The tests above call `customers_bulk_tag` directly, which proves handler
# behaviour but NOT the permission gate or the request-model shape — the
# adversarial audit pointed out that a PR claiming both left both unproven.


def test_route_refuses_a_caller_without_customers_write():
    from fastapi.testclient import TestClient

    from gdx_dispatch.app import app

    with TestClient(app) as client:
        r = client.post("/api/customers/bulk-tag",
                        json={"customer_ids": [str(uuid.uuid4())], "tag": "VIP"})
    assert r.status_code in (401, 403), (
        f"an unauthenticated caller reached a customer mutation: {r.status_code}"
    )


@pytest.mark.parametrize("body", [
    {"customer_ids": [], "tag": "VIP"},          # empty selection
    {"customer_ids": ["x"], "tag": ""},          # blank tag
    {"customer_ids": ["x"]},                     # missing tag
    {"tag": "VIP"},                              # missing ids
])
def test_route_rejects_malformed_bodies_before_touching_the_db(body):
    """The stub took a raw `dict` and validated nothing. These are 422 (or the
    auth refusal, whichever the dependency order reaches first) — never 200."""
    from fastapi.testclient import TestClient

    from gdx_dispatch.app import app

    with TestClient(app) as client:
        r = client.post("/api/customers/bulk-tag", json=body)
    assert r.status_code != 200, f"malformed body accepted: {body}"


def test_a_non_canonical_uuid_still_writes_a_readable_assignment(db):
    """Regression for the audit's finding: `entity_id` is free-form text, so
    the caller's SPELLING of the id used to be stored verbatim. An uppercase
    UUID wrote a row `_list_tags_for_entity` could never read — and a second
    call with the canonical form added a DUPLICATE, defeating idempotency.

    Not reachable from SegmentsView (API ids serialize lowercase); reachable
    from any other API client, which is why it is pinned.
    """
    from gdx_dispatch.routers.tags import _list_tags_for_entity

    cid = _customer(db, "A")

    _call(db, [cid.upper()], "VIP")

    assert [a.entity_id for a in _assignments(db)] == [cid], "entity_id was not canonicalised"
    assert _list_tags_for_entity(db, TENANT, "customer", cid), "the assignment is unreadable"

    # the canonical spelling must not add a second row
    _call(db, [cid], "VIP")
    assert len(_assignments(db)) == 1, "non-canonical + canonical produced duplicates"


def test_one_failing_assignment_does_not_abandon_the_batch(db, monkeypatch):
    """No transaction spans the loop — `_assign_tag` commits per assignment.
    A raise partway used to return 500 and lose the per-customer reporting
    that is the whole point of this handler."""
    ids = [_customer(db, "A"), _customer(db, "B"), _customer(db, "C")]
    real = __import__("gdx_dispatch.routers.tags", fromlist=["_assign_tag"])._assign_tag
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr("gdx_dispatch.routers.tags._assign_tag", flaky)

    out = _call(db, ids, "VIP")

    assert out["tagged"] == 2, "the batch stopped at the failure instead of continuing"
    assert len(out["failed"]) == 1, f"the failure was not reported: {out}"
    assert len(_assignments(db)) == 2
