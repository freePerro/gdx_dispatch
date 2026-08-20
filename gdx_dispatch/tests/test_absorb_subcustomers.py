"""Customers that were never customers become jobsites on their parent.

QuickBooks models a project as a sub-customer, and the pull used to read only
DisplayName — so each one became a TOP-LEVEL GDX customer. The account's
invoice history stayed on the parent while new work accumulated on the
fragments. The pull no longer does that; it also does not undo it. This does.

Deliberately NOT a merge. A merge says "these two rows are the same
customer"; this says "this row was never a customer, it was a job AT that
customer" — so the name survives as a jobsite label rather than being
discarded, and estimates that carried no jobsite text inherit it.

Pinned here:

* work moves to the parent and nothing is left behind;
* the sub-customer's NAME survives as the site label — that is the whole
  difference from a merge;
* sites are never `is_primary`; a primary location replaces the jobsite for
  every unbound job on the account (core/job_site.py);
* soft-delete only (invariant #2), with an audit row carrying enough
  pre-state to reverse it by hand;
* the QuickBooks map is repointed, so the next pull sees a site rather than a
  customer to re-create;
* a customer cannot absorb itself;
* the endpoint is permission-gated — it destroys the customer list otherwise.
"""
from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer, CustomerLocation, Job
from gdx_dispatch.routers.customers import AbsorbIn, absorb_subcustomers

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
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": TENANT}),
        headers={}, client=SimpleNamespace(host="127.0.0.1"),
    )


def _customer(db, name, address=None):
    c = Customer(id=uuid.uuid4(), name=name, address=address,
                 company_id=TENANT, created_at=datetime.now(UTC))
    db.add(c)
    db.commit()
    return c


def _job(db, customer, title="Spring repair"):
    j = Job(id=uuid.uuid4(), customer_id=customer.id, title=title,
            company_id=TENANT, created_at=datetime.now(UTC))
    db.add(j)
    db.commit()
    return j


def _qb_map(db, customer_id, qb_id):
    db.execute(text(
        "INSERT INTO qb_entity_maps (id, tenant_id, entity_type, local_id, qb_id, synced_at) "
        "VALUES (:i, :t, 'customer', :l, :q, :ts)"),
        {"i": uuid.uuid4().hex, "t": TENANT, "l": str(customer_id), "q": qb_id,
         "ts": datetime.now(UTC)})
    db.commit()


def _absorb(db, parent, subs):
    return absorb_subcustomers(
        str(parent.id), AbsorbIn(customer_ids=[str(s.id) for s in subs]),
        _req(), USER, db)


def _locations(db, parent):
    return db.execute(
        select(CustomerLocation).where(
            CustomerLocation.customer_id == str(parent.id),
            CustomerLocation.deleted_at.is_(None),
        )
    ).scalars().all()


# ── the core move ───────────────────────────────────────────────────────────


def test_a_subcustomer_becomes_a_named_site_on_the_parent(db):
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")

    out = _absorb(db, parent, [sub])

    sites = _locations(db, parent)
    assert [s.label for s in sites] == ["Site A"], "the job's NAME is the point"
    assert out.sites[0].customer_name == "Site A"
    assert out.sites[0].location_id == str(sites[0].id)


def test_the_name_survives_which_is_the_whole_difference_from_a_merge(db):
    """A merge discards the losing row's name. Here it is the label — without
    it the office loses the only record of which job the work belonged to."""
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Volden Field Shop")
    _absorb(db, parent, [sub])
    assert _locations(db, parent)[0].label == "Volden Field Shop"


def test_the_site_is_never_primary(db):
    """A primary location replaces the jobsite for every unbound job on the
    account (core/job_site.py) — on a site with no address that turns a real
    address into 'address missing'."""
    parent = _customer(db, "Riverbend Lumber")
    _absorb(db, parent, [_customer(db, "Site A"), _customer(db, "Site B")])
    assert all(not s.is_primary for s in _locations(db, parent))


def test_the_subcustomers_jobs_move_to_the_parent_and_bind_to_the_site(db):
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    job = _job(db, sub)

    out = _absorb(db, parent, [sub])

    db.expire_all()
    moved = db.execute(
        text("SELECT CAST(customer_id AS TEXT), location_id FROM jobs WHERE id = :i"),
        {"i": job.id.hex},
    ).first()
    assert uuid.UUID(moved[0]) == parent.id, "the work follows the account"
    assert moved[1] == out.sites[0].location_id, "and it remembers which job it was"


def test_an_estimate_with_no_jobsite_inherits_the_job_name(db):
    """Estimates carry free-text `jobsite_address` and no location_id, so the
    label is the only place the job name can land."""
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    # ORM rather than raw INSERT: the table carries several NOT NULL columns
    # with model-side defaults, and this test is about jobsite text, not about
    # rediscovering the estimate schema one constraint at a time.
    from gdx_dispatch.modules.proposals.models import Estimate

    for n, jobsite in enumerate((None, "TBD", "12 Oak St")):
        db.add(Estimate(id=uuid.uuid4(), estimate_number=f"EST-9000{n}",
                        customer_id=sub.id, company_id=TENANT,
                        public_token=uuid.uuid4().hex,
                        jobsite_address=jobsite, created_at=datetime.now(UTC)))
    db.commit()

    _absorb(db, parent, [sub])

    rows = db.execute(text("SELECT jobsite_address FROM estimates ORDER BY jobsite_address")).scalars().all()
    assert sorted(rows) == ["12 Oak St", "Site A", "Site A"], rows


# ── it stays reversible and honest ─────────────────────────────────────────


def test_the_subcustomer_is_soft_deleted_never_dropped(db):
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    _absorb(db, parent, [sub])

    row = db.execute(
        text("SELECT deleted_at FROM customers WHERE id = :i"), {"i": sub.id.hex}
    ).first()
    assert row is not None, "invariant #2 — the row stays"
    assert row[0] is not None


def test_the_audit_row_carries_enough_to_reverse_it(db):
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    _absorb(db, parent, [sub])

    details = db.execute(text(
        "SELECT details FROM audit_logs WHERE action = 'absorb_subcustomers_as_sites'"
    )).scalar()
    blob = str(details)
    assert str(sub.id) in blob, "which customer"
    assert "Site A" in blob, "under what name"
    assert "rows_updated" in blob, "and what moved"


def test_the_quickbooks_map_is_repointed_so_the_next_pull_sees_a_site(db):
    """Left as entity_type='customer', the next pull would take the legacy
    branch forever — or, worse, re-create the row it just retired."""
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    _qb_map(db, parent.id, "35")
    _qb_map(db, sub.id, "140")

    out = _absorb(db, parent, [sub])

    row = db.execute(text(
        "SELECT entity_type, local_id FROM qb_entity_maps WHERE qb_id = '140'"
    ).columns()).first()
    assert row[0] == "customer_location"
    assert row[1] == out.sites[0].location_id


def test_a_customer_cannot_absorb_itself(db):
    parent = _customer(db, "Riverbend Lumber")
    with pytest.raises(HTTPException) as exc:
        absorb_subcustomers(str(parent.id), AbsorbIn(customer_ids=[str(parent.id)]),
                            _req(), USER, db)
    assert exc.value.status_code == 422


def test_an_unknown_customer_is_refused_before_anything_moves(db):
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    ghost = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        absorb_subcustomers(str(parent.id), AbsorbIn(customer_ids=[str(sub.id), ghost]),
                            _req(), USER, db)
    assert exc.value.status_code == 404
    assert _locations(db, parent) == [], "nothing may move on a partial batch"


def test_absorbing_several_at_once_gives_each_its_own_site(db):
    parent = _customer(db, "Riverbend Lumber")
    subs = [_customer(db, n) for n in ("Site A", "Site B", "Site C")]
    out = _absorb(db, parent, subs)
    assert len(out.sites) == 3
    assert sorted(s.label for s in _locations(db, parent)) == ["Site A", "Site B", "Site C"]


def test_the_endpoint_requires_the_customers_write_permission():
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
    assert ("/api/customers/{parent_id}/absorb", "POST") in gated


# ── the audit's fixes, pinned ──────────────────────────────────────────────


def test_a_record_with_its_own_invoices_is_refused(db):
    """A jobsite does not have its own billing. The screen that offers this
    groups on a shared email, which on the real tenant is seven separate
    builders' accounts reached through one contact — and the operator picks
    the keeper from a radio button. Nothing else stops them folding the
    account with fifteen invoices into the shed."""
    from gdx_dispatch.models.tenant_models import Invoice

    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Actually An Account")
    db.add(Invoice(id=uuid.uuid4(), invoice_number="INV-90001", customer_id=sub.id,
                   company_id=TENANT, total=100, public_token=uuid.uuid4().hex,
                   created_at=datetime.now(UTC)))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _absorb(db, parent, [sub])
    assert exc.value.status_code == 422
    assert "not a jobsite" in str(exc.value.detail)
    assert _locations(db, parent) == [], "refused before anything moved"


def test_an_existing_site_map_does_not_collide(db):
    """qb_entity_maps carries UNIQUE (tenant_id, entity_type, qb_id). The QB
    pull already writes a `customer_location` map for a sub-customer's qb_id,
    so on any tenant that has synced since #373, re-pointing the old
    `customer` row onto the same qb_id violates that constraint — 500, roll
    back, and identically on every retry. The feature would be permanently
    dead on exactly the tenants that had synced."""
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    _qb_map(db, sub.id, "140")
    # the pull got there first and already made a site for qb 140
    db.execute(text(
        "INSERT INTO qb_entity_maps (id, tenant_id, entity_type, local_id, qb_id, synced_at) "
        "VALUES (:i, :t, 'customer_location', :l, '140', :ts)"),
        {"i": uuid.uuid4().hex, "t": TENANT, "l": str(uuid.uuid4()), "ts": datetime.now(UTC)})
    db.commit()

    _absorb(db, parent, [sub])  # must not raise

    kinds = db.execute(text(
        "SELECT entity_type FROM qb_entity_maps WHERE qb_id = '140'")).scalars().all()
    assert kinds == ["customer_location"], kinds
    assert not db.execute(text(
        "SELECT 1 FROM qb_entity_maps WHERE entity_type = 'customer' AND local_id = :l"),
        {"l": str(sub.id)}).first(), "the stale customer mapping must be gone"


def test_a_failure_rolls_everything_back(db):
    """The 'nothing moves on a partial batch' test only exercised a 404 raised
    BEFORE the first mutation — it could not fail. This one breaks the run
    after sites and moves are already staged."""
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    job = _job(db, sub)

    import gdx_dispatch.routers.customers as mod
    original = mod.audit_or_rollback
    mod.audit_or_rollback = lambda *a, **k: (_ for _ in ()).throw(
        __import__("sqlalchemy").exc.SQLAlchemyError("audit down"))
    try:
        with pytest.raises(HTTPException):
            _absorb(db, parent, [sub])
    finally:
        mod.audit_or_rollback = original

    db.expire_all()
    assert _locations(db, parent) == [], "no orphan site may survive"
    still = db.execute(text("SELECT deleted_at FROM customers WHERE id = :i"),
                       {"i": sub.id.hex}).scalar()
    assert still is None, "the customer must still be live"
    owner = db.execute(text("SELECT CAST(customer_id AS TEXT) FROM jobs WHERE id = :i"),
                       {"i": job.id.hex}).scalar()
    assert uuid.UUID(owner) == sub.id, "the work must not have moved"


def test_the_audit_records_which_rows_moved_not_just_how_many(db):
    """"invoices.customer_id: 15" cannot be reversed — it does not say which
    fifteen of the parent's invoices arrived from the sub. The dialog tells
    the operator this is undoable by hand; the trail has to support that."""
    parent = _customer(db, "Riverbend Lumber")
    sub = _customer(db, "Site A")
    job = _job(db, sub)
    _absorb(db, parent, [sub])

    details = str(db.execute(text(
        "SELECT details FROM audit_logs WHERE action = 'absorb_subcustomers_as_sites'"
    )).scalar())
    assert "moved_row_ids" in details
    assert str(job.id) in details or job.id.hex in details


def test_the_fk_sweep_follows_declared_foreign_keys(db):
    """Matching by column NAME missed `outlook_messages.linked_customer_id` —
    a real FK holding 405 rows on this tenant, 26 of them on the account this
    feature exists for — which would have been stranded on a soft-deleted
    customer where no screen shows it again."""
    from gdx_dispatch.routers.customers import _MERGE_TABLES_CACHE, _discover_customer_fk_tables

    _MERGE_TABLES_CACHE.pop("tables", None)
    pairs = _discover_customer_fk_tables(db)
    _MERGE_TABLES_CACHE.pop("tables", None)
    cols = {c for _, c in pairs}
    assert "customer_id" in cols
    # any declared FK column, whatever it is called, must be discovered
    assert any(c != "customer_id" for c in cols), (
        f"only name-matched columns found: {sorted(cols)}"
    )
