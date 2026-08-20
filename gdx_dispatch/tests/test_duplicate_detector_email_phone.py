"""The duplicate screen sees shared emails and phones, not just names.

The screen that exists to catch duplicate customers grouped on normalized NAME
alone. QuickBooks sub-customers arrive as separate top-level customers
carrying the PARENT's email, so six job names under one lumber-yard account
had six different names and one shared address — invisible to a name-only
detector. On this tenant 29 of 306 active customers share an email with
another row, and not one was surfaced.

Pinned here:

* groups form on shared email and shared phone, not only shared name;
* each group says WHAT matched, because a name match and an email match want
  opposite treatment — one is the same account twice, the other is usually one
  account's separate jobs, which must not be blindly merged;
* the same set of records is never shown twice under two signals;
* a phone match needs enough digits to mean something;
* blank emails and blank phones never group anybody.
"""
from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.customers import list_duplicates

TENANT = "tenant-test"
USER = {"sub": "office-1"}


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _req():
    return SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}))


def _add(db, name, email=None, phone=None):
    c = Customer(id=uuid.uuid4(), name=name, email=email, phone=phone,
                 company_id=TENANT, created_at=datetime.now(UTC))
    db.add(c)
    db.commit()
    return c


def _groups(db):
    return list_duplicates(_req(), USER, db).groups


def _by_match(groups, kind):
    return [g for g in groups if g.match_on == kind]


# ── the gap this closes ─────────────────────────────────────────────────────


def test_a_shared_email_forms_a_group_even_with_different_names(db):
    """The lumber-yard case: six job names, one account email, six different
    names. A name-only detector shows nothing."""
    for label in ("Site A", "Site B", "Site C"):
        _add(db, label, email="shared@example.invalid")

    email_groups = _by_match(_groups(db), "email")
    assert len(email_groups) == 1
    assert email_groups[0].count == 3
    assert email_groups[0].match_value == "shared@example.invalid"
    assert sorted(m.name for m in email_groups[0].members) == ["Site A", "Site B", "Site C"]


def test_a_shared_phone_forms_a_group_across_formatting(db):
    """Digits only — the same number typed three ways is one number."""
    _add(db, "Someone", phone="(218) 555-0100")
    _add(db, "Someone Else", phone="218-555-0100")
    _add(db, "A Third", phone="2185550100")

    phone_groups = _by_match(_groups(db), "phone")
    assert len(phone_groups) == 1
    assert phone_groups[0].count == 3


def test_every_group_says_what_matched(db):
    """A reviewer who cannot tell a name match from an email match is
    guessing, and the two want opposite treatment."""
    _add(db, "Troy Example", email="troy@example.invalid")
    _add(db, "Troy Example", email="troy@example.invalid")
    _add(db, "Site A", email="shared@example.invalid")
    _add(db, "Site B", email="shared@example.invalid")

    groups = _groups(db)
    assert {g.match_on for g in groups} == {"name", "email"}
    for g in groups:
        assert g.match_value, "a group must name the value that tied it together"


# ── it stays honest ─────────────────────────────────────────────────────────


def test_the_same_records_are_not_listed_twice_under_two_signals(db):
    """Two records with the same name AND the same email are one problem."""
    _add(db, "Troy Example", email="troy@example.invalid", phone="218-555-0100")
    _add(db, "Troy Example", email="troy@example.invalid", phone="218-555-0100")

    groups = _groups(db)
    assert len(groups) == 1, [g.match_on for g in groups]
    assert groups[0].match_on == "name", "the strongest signal wins the listing"


def test_a_name_group_and_an_email_group_with_different_members_both_show(db):
    _add(db, "Troy Example")
    _add(db, "Troy Example")
    _add(db, "Site A", email="shared@example.invalid")
    _add(db, "Site B", email="shared@example.invalid")
    assert len(_groups(db)) == 2


def test_blank_contact_details_never_group_anybody(db):
    """Empty is not a match — three customers with no email are not a trio."""
    for name in ("Alpha", "Beta", "Gamma"):
        _add(db, name, email="", phone=None)
    assert _groups(db) == []


def test_a_short_phone_fragment_does_not_group(db):
    """Last-4 matching was rejected: on a 300-customer book it puts strangers
    in the same group, and a reviewer who stops trusting the screen stops
    using it."""
    _add(db, "Alpha", phone="0100")
    _add(db, "Beta", phone="0100")
    assert _by_match(_groups(db), "phone") == []


def test_a_lone_customer_is_never_a_group(db):
    _add(db, "Only One", email="only@example.invalid", phone="218-555-0100")
    assert _groups(db) == []


def test_names_group_case_and_whitespace_insensitively(db):
    _add(db, "Troy Example")
    _add(db, "  troy   example ")
    name_groups = _by_match(_groups(db), "name")
    assert len(name_groups) == 1
    assert name_groups[0].count == 2


def test_soft_deleted_customers_are_not_offered_for_merge(db):
    a = _add(db, "Troy Example", email="troy@example.invalid")
    _add(db, "Troy Example", email="troy@example.invalid")
    a.deleted_at = datetime.now(UTC)
    db.commit()
    assert _groups(db) == []


def test_members_carry_the_evidence_a_reviewer_picks_by(db):
    """These counts decide which record survives a merge, and they used to be
    silently zero on any non-Postgres dialect: the queries used `= ANY(:ids)`,
    a Postgres array operator, and the surrounding except branch swallowed the
    failure. Asserting they are zero would have passed on the broken version —
    so seed real rows and assert they are FOUND."""
    from gdx_dispatch.models.tenant_models import Job

    keeper = _add(db, "Troy Example", email="troy@example.invalid")
    _add(db, "Troy Example", email="troy@example.invalid")
    db.add(Job(id=uuid.uuid4(), customer_id=keeper.id, title="Spring repair",
               company_id=TENANT, created_at=datetime.now(UTC)))
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO qb_entity_maps (id, tenant_id, entity_type, local_id, qb_id, synced_at) "
            "VALUES (:i, :t, 'customer', :l, '35', :ts)"),
        {"i": uuid.uuid4().hex, "t": TENANT, "l": str(keeper.id), "ts": datetime.now(UTC)})
    db.commit()

    members = {m.id: m for m in _groups(db)[0].members}
    kept = members[str(keeper.id)]
    assert kept.job_count == 1, "job evidence must reach the reviewer"
    assert kept.has_qb_link is True, "QB-link evidence must reach the reviewer"


def test_the_group_key_stays_unique_across_match_types(db):
    """The UI tracks per-group selections by `normalized_name` AND uses it as
    the radio-button group name, so two groups sharing a key would submit one
    card's ids from the other card's button.

    The collision needs a customer NAMED exactly like the prefixed key an
    email group would produce. An earlier version of this test seeded a
    customer named "shared@example.invalid" against an email group for the
    same address — keys "shared@…" and "email:shared@…", different by
    construction, so it could not fail. This builds the real one.
    """
    _add(db, "email:shared@example.invalid")
    _add(db, "email:shared@example.invalid")
    _add(db, "Site A", email="shared@example.invalid")
    _add(db, "Site B", email="shared@example.invalid")

    keys = [g.normalized_name for g in _groups(db)]
    assert len(keys) == len(set(keys)), f"COLLISION: {keys}"


def test_a_customer_is_never_listed_in_two_groups(db):
    """Deduping on whole member SETS only catches an exact repeat. Partial
    overlap — two records share a name, a third shares just their email — put
    the same customer in two cards, where two keep/merge choices can disagree
    about where its invoices go."""
    _add(db, "Bob Yard", email="yard@example.invalid")
    _add(db, "Bob Yard", email="yard@example.invalid")
    _add(db, "Different Job", email="yard@example.invalid")

    seen: dict[str, int] = {}
    for g in _groups(db):
        for m in g.members:
            seen[m.id] = seen.get(m.id, 0) + 1
    repeated = {k: v for k, v in seen.items() if v > 1}
    assert not repeated, f"customers listed in more than one group: {repeated}"


def test_both_endpoints_require_the_customers_write_permission(db):
    """`/duplicates` returns every active customer's name, email, phone and
    decrypted address unpaginated, and `/merge` destroys billing history.
    Both took a bare get_current_user — any authenticated session, a
    technician's phone included."""
    from gdx_dispatch.routers.customers import router

    gated = {
        (r.path, m)
        for r in router.routes
        if hasattr(r, "methods")
        for m in r.methods
        if any(
            getattr(d.dependency, "__qualname__", "").startswith("require_permission")
            for d in getattr(r, "dependencies", [])
        )
    }
    assert ("/api/customers/duplicates", "GET") in gated
    assert ("/api/customers/merge", "POST") in gated
