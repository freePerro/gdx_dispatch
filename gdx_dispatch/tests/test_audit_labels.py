"""Tests for gdx_dispatch/core/audit_labels.py — the shared actor/subject
resolver behind both audit feeds.

``audit_logs`` stores opaque ids. Before this module the dashboard could only
render "Data Accessed (customer)" against a UUID, and a portal login was
attributed to "Unknown user (a1b2c3d4)" because the resolver only looked in
``users``. These tests pin both halves.
"""
from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core import audit_labels
from gdx_dispatch.tests.conftest import make_fresh_db


@pytest.fixture()
def db():
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


def _mk_customer(db, name="Acme Storage"):
    from gdx_dispatch.models.tenant_models import Customer

    c = Customer(id=uuid.uuid4(), name=name, company_id="tenant-a")
    db.add(c)
    db.commit()
    return c


def _mk_user(db, **kw):
    from gdx_dispatch.models.tenant_models import User

    defaults = dict(id=uuid.uuid4(), company_id="tenant-a", role="admin")
    defaults.update(kw)
    u = User(**defaults)
    db.add(u)
    db.commit()
    return u


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


def test_machine_actor_is_labelled_system(db):
    out = audit_labels.resolve_actors(db, {"system"})
    assert out["system"]["name"] == "System"
    assert out["system"]["actor_type"] == audit_labels.ACTOR_SYSTEM


def test_staff_actor_resolves_to_display_name(db):
    u = _mk_user(db, name="Amber Joy", email="amber@example.com")
    out = audit_labels.resolve_actors(db, {str(u.id)})
    assert out[str(u.id)]["name"] == "Amber Joy"
    assert out[str(u.id)]["actor_type"] == audit_labels.ACTOR_STAFF


def test_nameless_staff_user_is_not_rendered_as_a_hex_string(db):
    """The old resolver fell back to str(id)[:8], an 8-char hex blob that
    renders as if it were a person's name. It must announce itself instead."""
    u = _mk_user(db, name=None, full_name=None, email=None)
    out = audit_labels.resolve_actors(db, {str(u.id)})
    assert out[str(u.id)]["name"].startswith("Unnamed user (")
    assert out[str(u.id)]["actor_type"] == audit_labels.ACTOR_STAFF


def test_deleted_principal_is_named_unknown_not_a_bare_uuid(db):
    ghost = str(uuid.uuid4())
    out = audit_labels.resolve_actors(db, {ghost})
    assert out[ghost]["name"] == f"Unknown user ({ghost[:8]})"
    assert out[ghost]["actor_type"] == audit_labels.ACTOR_UNKNOWN


def test_api_key_actor_is_its_own_class_not_an_error(db):
    out = audit_labels.resolve_actors(db, {"gdx_live_0f26c7d"})
    assert out["gdx_live_0f26c7d"]["name"] == "gdx_live_0f26c7d"
    assert out["gdx_live_0f26c7d"]["actor_type"] == audit_labels.ACTOR_API_KEY


def test_portal_customer_user_resolves_as_a_customer_not_unknown(db):
    """The regression this whole phase exists for: routers/portal.py writes
    user_id = customer_user.id, which is not a row in `users`."""
    from gdx_dispatch.modules.customer_portal.models import CustomerUser

    cust = _mk_customer(db, name="Beta Freight")
    cu = CustomerUser(id=uuid.uuid4(), customer_id=cust.id, email="ops@beta.example")
    db.add(cu)
    db.commit()

    out = audit_labels.resolve_actors(db, {str(cu.id)})
    assert out[str(cu.id)]["actor_type"] == audit_labels.ACTOR_CUSTOMER
    # The person (email), NOT the company — see
    # test_two_contacts_at_one_customer_are_different_actors. The company is
    # already the row's subject, so repeating it here would stutter.
    assert out[str(cu.id)]["name"] == "ops@beta.example"
    assert "Unknown user" not in out[str(cu.id)]["name"]


def test_slug_machine_actors_are_system_not_api_keys(db):
    """bounce_detect.py writes user_id="bounce-detector" (a slug, not a
    UUID). Before this it fell into the API-key class and the estimate
    activity panel showed an API key called "bounce-detector"."""
    out = audit_labels.resolve_actors(db, {"bounce-detector", "resend-detector"})
    assert out["bounce-detector"] == {
        "name": "System — email bounce detector", "actor_type": audit_labels.ACTOR_SYSTEM,
    }
    assert out["resend-detector"]["actor_type"] == audit_labels.ACTOR_SYSTEM


def test_public_link_and_portal_prefixed_actors_read_as_the_customer(db):
    """modules/proposals/router.py writes "customer:public-link"; portal.py
    writes "portal:<CustomerUser id>". Both were API keys named by the raw
    string; the portal one now resolves the login (email), and a deleted
    login degrades to a generic customer instead of "Unknown user"."""
    from gdx_dispatch.modules.customer_portal.models import CustomerUser

    cust = _mk_customer(db, name="Gamma Farms")
    cu = CustomerUser(id=uuid.uuid4(), customer_id=cust.id, email="pat@gamma.example")
    db.add(cu)
    db.commit()
    gone = uuid.uuid4()

    out = audit_labels.resolve_actors(
        db, {"customer:public-link", f"portal:{cu.id}", f"portal:{gone}", "portal:not-a-uuid"}
    )
    assert out["customer:public-link"] == {
        "name": "Customer (email link)", "actor_type": audit_labels.ACTOR_CUSTOMER,
    }
    assert out[f"portal:{cu.id}"] == {
        "name": "pat@gamma.example (portal)", "actor_type": audit_labels.ACTOR_CUSTOMER,
    }
    assert out[f"portal:{gone}"] == {"name": "Customer (portal)", "actor_type": audit_labels.ACTOR_CUSTOMER}
    assert out["portal:not-a-uuid"]["actor_type"] == audit_labels.ACTOR_CUSTOMER
    for v in out.values():
        assert v["actor_type"] != audit_labels.ACTOR_API_KEY
        assert "Unknown user" not in v["name"]


def test_mixed_actor_page_resolves_in_one_pass(db):
    u = _mk_user(db, name="Dispatch")
    ghost = str(uuid.uuid4())
    out = audit_labels.resolve_actors(db, {str(u.id), "system", "gdx_live_x", ghost})
    assert {v["actor_type"] for v in out.values()} == {
        audit_labels.ACTOR_STAFF,
        audit_labels.ACTOR_SYSTEM,
        audit_labels.ACTOR_API_KEY,
        audit_labels.ACTOR_UNKNOWN,
    }


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


def test_every_deep_link_prefix_is_a_real_vue_route():
    """The one test that would have caught the dead invoice link.

    audit_labels hardcodes SPA paths. Nothing else in CI connects a backend
    string to the Vue router, so a route rename would silently turn every
    activity row into a 404. `/invoices/:id` is exactly that trap: `/invoices`
    is a redirect stub and the detail view lives at `/billing/:id`.
    """
    import re

    router = (
        pathlib.Path(__file__).resolve().parents[1]
        / "frontend" / "src" / "router" / "index.js"
    ).read_text()

    # Every `path: '/x/:id'`-style detail route declared in the SPA.
    detail_prefixes = {
        m.group(1)
        for m in re.finditer(r"path:\s*'(/[a-z0-9\-]+)/:id'", router)
    }
    # Plain list routes, for prefixes we link to without an id (e.g. /leads).
    list_paths = {
        m.group(1)
        for m in re.finditer(r"path:\s*'(/[a-z0-9\-]+)'\s*,\s*name:", router)
    }

    assert detail_prefixes, "could not parse any detail routes — test is broken"

    for entity, prefix in audit_labels.ENTITY_ROUTE_PREFIXES.items():
        assert prefix in detail_prefixes or prefix in list_paths, (
            f"audit_labels links {entity} to {prefix!r}, which is not a route "
            f"in the Vue router. Detail routes: {sorted(detail_prefixes)}"
        )


def test_invoice_links_to_billing_not_invoices(db):
    """Regression: /invoices/:id is a redirect stub, not a detail route."""
    from gdx_dispatch.models.tenant_models import Invoice

    c = _mk_customer(db)
    inv = Invoice(
        id=uuid.uuid4(),
        customer_id=c.id,
        invoice_number="INV-000123",
        company_id="tenant-a",
        public_token=uuid.uuid4().hex,
    )
    db.add(inv)
    db.commit()

    out = audit_labels.resolve_entity_labels(db, {("invoice", str(inv.id))})
    got = out[("invoice", str(inv.id))]
    assert got["label"] == "INV-000123"
    assert got["url"] == f"/billing/{inv.id}"
    assert not got["url"].startswith("/invoices/")


def test_two_contacts_at_one_customer_are_different_actors(db):
    """Attribution must identify the person, not collapse to the company.

    `Customer.name` alone made every contact at a company the same actor,
    which defeats the entire point of asking who did something.
    """
    from gdx_dispatch.modules.customer_portal.models import CustomerUser

    cust = _mk_customer(db, name="Beta Freight")
    a = CustomerUser(id=uuid.uuid4(), customer_id=cust.id, email="ops@beta.example")
    b = CustomerUser(id=uuid.uuid4(), customer_id=cust.id, email="ap@beta.example")
    db.add_all([a, b])
    db.commit()

    out = audit_labels.resolve_actors(db, {str(a.id), str(b.id)})
    assert out[str(a.id)]["name"] != out[str(b.id)]["name"]
    assert out[str(a.id)]["name"] == "ops@beta.example"
    assert out[str(b.id)]["name"] == "ap@beta.example"


def test_non_canonical_uuid_actor_resolves_under_the_raw_key(db):
    """UUID() accepts uppercase/braced/unhyphenated forms. Resolution keys on
    the canonical string, but callers read back the raw value — so a
    non-canonical id resolved correctly and was then reported as unknown."""
    u = _mk_user(db, name="Amber Joy")
    raw = str(u.id).upper()
    out = audit_labels.resolve_actors(db, {raw})
    assert out[raw]["name"] == "Amber Joy"
    assert out[raw]["actor_type"] == audit_labels.ACTOR_STAFF


def test_customer_entity_resolves_to_name_and_deep_link(db):
    c = _mk_customer(db, name="Acme Storage")
    out = audit_labels.resolve_entity_labels(db, {("customer", str(c.id))})
    assert out[("customer", str(c.id))]["label"] == "Acme Storage"
    assert out[("customer", str(c.id))]["url"] == f"/customers/{c.id}"


def test_unknown_entity_type_is_absent_not_an_error(db):
    out = audit_labels.resolve_entity_labels(db, {("qb_webhook", "abc-123")})
    assert out == {}


def test_non_uuid_entity_id_does_not_blow_up_the_page(db):
    """Postgres refuses to cast 'job-1' to uuid; an unguarded IN clause would
    take the whole feed down with it."""
    c = _mk_customer(db)
    out = audit_labels.resolve_entity_labels(
        db, {("customer", "job-1"), ("customer", str(c.id))}
    )
    assert out[("customer", str(c.id))]["label"] == c.name
    assert ("customer", "job-1") not in out


def test_missing_entity_row_is_absent_so_ui_falls_back(db):
    out = audit_labels.resolve_entity_labels(db, {("customer", str(uuid.uuid4()))})
    assert out == {}


# ---------------------------------------------------------------------------
# decorate_rows — the entry point both routers use
# ---------------------------------------------------------------------------


def test_decorate_rows_adds_all_four_fields(db):
    c = _mk_customer(db, name="Acme Storage")
    u = _mk_user(db, name="Amber Joy")
    rows = [
        {
            "id": "1",
            "user_id": str(u.id),
            "action": "data_accessed",
            "entity_type": "customer",
            "entity_id": str(c.id),
        }
    ]
    out = audit_labels.decorate_rows(db, rows)
    assert out[0]["user_name"] == "Amber Joy"
    assert out[0]["actor_type"] == audit_labels.ACTOR_STAFF
    assert out[0]["entity_label"] == "Acme Storage"
    assert out[0]["entity_url"] == f"/customers/{c.id}"


def test_decorate_rows_leaves_unresolvable_subject_as_none(db):
    rows = [
        {
            "id": "1",
            "user_id": "system",
            "action": "qb_webhook_received",
            "entity_type": "qb_webhook",
            "entity_id": "evt-1",
        }
    ]
    out = audit_labels.decorate_rows(db, rows)
    assert out[0]["entity_label"] is None
    assert out[0]["entity_url"] is None
    assert out[0]["user_name"] == "System"


def test_decorate_rows_on_empty_page_is_a_noop(db):
    assert audit_labels.decorate_rows(db, []) == []


def test_decorate_rows_is_batched_not_n_plus_one(db):
    """50 rows across 2 entity types must not issue 50 queries."""
    from sqlalchemy import event

    customers = [_mk_customer(db, name=f"Cust {i}") for i in range(10)]
    u = _mk_user(db, name="Dispatch")
    rows = [
        {
            "id": str(i),
            "user_id": str(u.id),
            "action": "data_accessed",
            "entity_type": "customer",
            "entity_id": str(customers[i % 10].id),
        }
        for i in range(50)
    ]

    counter = {"n": 0}

    def _count(*_args, **_kwargs):
        counter["n"] += 1

    event.listen(db.get_bind(), "before_cursor_execute", _count)
    try:
        audit_labels.decorate_rows(db, rows)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", _count)

    # one actor query + one customer query, plus a little slack for SQLAlchemy
    # bookkeeping. The point is that it is O(types), not O(rows).
    assert counter["n"] <= 6, f"expected a handful of queries, got {counter['n']}"
