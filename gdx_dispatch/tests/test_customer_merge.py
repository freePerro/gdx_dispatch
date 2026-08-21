"""Merging two customers actually merges them.

This button had **never worked in production**. The soft-delete carried
`updated_at = :now` from the initial public release, and `customers` has never
had that column — not in the ORM, not in the Postgres schema. So on Postgres
every merge raised `UndefinedColumn`, rolled back, and returned the generic
"A database error occurred". The live tenant has **zero** `merge_customers`
audit rows, across every merge ever attempted.

Nothing caught it because nothing could: `_discover_customer_fk_tables` read
`information_schema`, which is Postgres-only, so any SQLite test of this path
raised before reaching the bug. That helper now falls back to the SQLAlchemy
inspector, which is what makes this file possible at all.

Pinned here:

* a merge moves the losers' work to the keeper and retires them;
* the losers are SOFT-deleted (invariant #2) — never dropped;
* the whole thing is one transaction: if the retirement cannot complete, the
  FK moves that already ran are rolled back, because a customer left live
  after its work moved is a customer that owns nothing;
* it writes an audit row naming who, what and when (invariant #1);
* it refuses a keeper that is also in the merge list.
"""
from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.routers.customers import MergeIn, merge_customers

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


@pytest.fixture(autouse=True)
def _clear_fk_cache():
    """The discovered FK list is process-cached; a stale one from another
    test's engine would silently narrow what this merge sweeps."""
    from gdx_dispatch.routers.customers import _MERGE_TABLES_CACHE

    _MERGE_TABLES_CACHE.pop("tables", None)
    yield
    _MERGE_TABLES_CACHE.pop("tables", None)


def _req():
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": TENANT}),
        headers={}, client=SimpleNamespace(host="127.0.0.1"),
    )


def _customer(db, name):
    c = Customer(id=uuid.uuid4(), name=name, company_id=TENANT,
                 created_at=datetime.now(UTC))
    db.add(c)
    db.commit()
    return c


def _job(db, customer, title="Spring repair"):
    j = Job(id=uuid.uuid4(), customer_id=customer.id, title=title,
            company_id=TENANT, created_at=datetime.now(UTC))
    db.add(j)
    db.commit()
    return j


def _merge(db, keep, losers):
    return merge_customers(
        MergeIn(keep_id=str(keep.id), merge_ids=[str(x.id) for x in losers]),
        _req(), USER, db)


def _live(db, customer):
    return db.execute(
        text("SELECT deleted_at FROM customers WHERE id = :i"), {"i": customer.id.hex}
    ).scalar() is None


# ── the bug ─────────────────────────────────────────────────────────────────


def test_a_merge_completes_instead_of_500ing(db):
    """The regression this file exists for. Before the fix the soft-delete
    named a column `customers` does not have, so this raised
    UndefinedColumn on Postgres and the operator saw "A database error
    occurred" with nothing merged."""
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")

    out = _merge(db, keep, [loser])

    assert out.keep_id == str(keep.id)
    assert out.merged_count == 1
    assert _live(db, keep)
    assert not _live(db, loser), "the loser must be retired"


def test_the_losers_work_moves_to_the_keeper(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    job = _job(db, loser)

    out = _merge(db, keep, [loser])

    owner = db.execute(
        text("SELECT CAST(customer_id AS TEXT) FROM jobs WHERE id = :i"), {"i": job.id.hex}
    ).scalar()
    assert uuid.UUID(owner) == keep.id
    assert out.rows_updated.get("jobs.customer_id") == 1


def test_the_loser_is_soft_deleted_never_dropped(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    _merge(db, keep, [loser])

    row = db.execute(
        text("SELECT id, deleted_at FROM customers WHERE id = :i"), {"i": loser.id.hex}
    ).first()
    assert row is not None, "invariant #2 — the row stays"
    assert row[1] is not None


def test_merging_several_at_once_retires_all_of_them(db):
    keep = _customer(db, "Troy Example")
    losers = [_customer(db, "Troy example"), _customer(db, "TROY EXAMPLE")]
    out = _merge(db, keep, losers)
    assert out.merged_count == 2
    assert all(not _live(db, x) for x in losers)


# ── it stays safe ───────────────────────────────────────────────────────────


def test_a_keeper_cannot_also_be_a_loser(db):
    keep = _customer(db, "Troy Example")
    with pytest.raises(HTTPException) as exc:
        merge_customers(MergeIn(keep_id=str(keep.id), merge_ids=[str(keep.id)]),
                        _req(), USER, db)
    assert exc.value.status_code in (400, 422)
    assert _live(db, keep)


def test_an_unknown_loser_is_refused_before_anything_moves(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    job = _job(db, loser)

    with pytest.raises(HTTPException) as exc:
        merge_customers(
            MergeIn(keep_id=str(keep.id), merge_ids=[str(loser.id), str(uuid.uuid4())]),
            _req(), USER, db)
    assert exc.value.status_code == 404

    owner = db.execute(
        text("SELECT CAST(customer_id AS TEXT) FROM jobs WHERE id = :i"), {"i": job.id.hex}
    ).scalar()
    assert uuid.UUID(owner) == loser.id, "nothing may move on a partial batch"
    assert _live(db, loser)


def test_a_failed_retirement_rolls_back_the_work_it_already_moved(db):
    """The FK moves run BEFORE the soft-delete. If the retirement cannot
    complete, a customer left live owns nothing — every job it had is now
    filed under someone else. That has to roll back."""
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    job = _job(db, loser)

    # Fail the audit WRITE, leaving audit_or_rollback and its
    # ensure_audit_table call intact. An earlier version replaced
    # audit_or_rollback wholesale — which also removed the lazy
    # ensure_audit_table inside it, the very thing that commits the staged
    # merge. That test proved only that `except SQLAlchemyError: rollback()`
    # runs, which was never in doubt, and passed on code where the rollback
    # was a lie.
    import gdx_dispatch.core.audit as audit_mod
    original = audit_mod._log_audit_event_impl

    def _boom(session, *a, **k):
        audit_mod.ensure_audit_table(session)
        raise __import__("sqlalchemy").exc.SQLAlchemyError("audit write failed")

    audit_mod._log_audit_event_impl = _boom
    try:
        with pytest.raises(HTTPException):
            _merge(db, keep, [loser])
    finally:
        audit_mod._log_audit_event_impl = original

    db.expire_all()
    owner = db.execute(
        text("SELECT CAST(customer_id AS TEXT) FROM jobs WHERE id = :i"), {"i": job.id.hex}
    ).scalar()
    assert uuid.UUID(owner) == loser.id, "the work must not stay moved"
    assert _live(db, loser), "and the customer must still be live"


def test_the_merge_leaves_an_audit_row(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    _merge(db, keep, [loser])

    row = db.execute(text(
        "SELECT user_id, details FROM audit_logs WHERE action = 'merge_customers'"
    )).first()
    assert row is not None, (
        "invariant #1 — and the live tenant has zero of these rows, which is "
        "how nobody noticed the button was dead"
    )
    # who, and what — not merely that some row exists
    assert row[0], "the audit row must name an actor"
    blob = str(row[1])
    assert str(loser.id) in blob, "which record was merged away"
    assert str(keep.id) in blob, "and into which keeper"


def test_the_fk_sweep_is_discoverable_without_information_schema(db):
    """`information_schema` is Postgres-only. When it raised, this whole path
    was untestable — which is why a column that does not exist survived in the
    soft-delete for two months."""
    from gdx_dispatch.routers.customers import _discover_customer_fk_tables

    pairs = _discover_customer_fk_tables(db)
    assert ("jobs", "customer_id") in pairs, pairs


def test_an_uppercase_uuid_still_resolves(db):
    """`CAST(id AS TEXT)` always yields lowercase, so passing a client id
    through verbatim made an UPPERCASE uuid — which the old
    `id = :id::uuid` comparison matched fine — resolve to zero rows and 404.
    Canonicalising through uuid.UUID is what keeps that working."""
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")

    out = merge_customers(
        MergeIn(keep_id=str(keep.id).upper(), merge_ids=[str(loser.id).upper()]),
        _req(), USER, db)

    assert out.merged_count == 1
    assert not _live(db, loser)


def test_the_keeper_id_is_stored_canonically(db):
    """The FK sweep writes `SET {column} = :keep` into varchar columns. A
    dash-less or uppercase keep_id would land verbatim and those rows would be
    invisible to every `customer_id = '<dashed>'` query in the app."""
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    job = _job(db, loser)

    merge_customers(
        MergeIn(keep_id=str(keep.id).upper(), merge_ids=[str(loser.id)]),
        _req(), USER, db)

    stored = db.execute(
        text("SELECT CAST(customer_id AS TEXT) FROM jobs WHERE id = :i"), {"i": job.id.hex}
    ).scalar()
    assert uuid.UUID(stored) == keep.id
    assert stored == stored.lower(), f"stored non-canonical: {stored}"
