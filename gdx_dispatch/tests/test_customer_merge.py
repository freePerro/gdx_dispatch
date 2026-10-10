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


def test_sql_identifier_is_the_gate_the_merge_noqa_leans_on():
    """merge/absorb interpolate catalog-supplied table and column names into
    `UPDATE {table} SET {column} …`, and those sites carry `# noqa: S608` on
    the strength of `_sql_identifier` in `_discover_customer_fk_tables`.
    The names come from information_schema / the inspector, never a request —
    this pins the gate as a real refusal so the noqa stays a guarantee."""
    from gdx_dispatch.routers.customers import _sql_identifier

    for good in ("jobs", "customer_id", "_x", "Table9", "related_customer_id"):
        assert _sql_identifier(good) == good
    for bad in ("", "jobs; DROP TABLE users", "a b", 'x"y', "9abc", "cust-id", None, "jobs\n"):
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            _sql_identifier(bad)


# ── reseller data is private to its customer (GDXA-490) ─────────────────────
#
# `reseller_profiles` and `resale_quotes` belong to the contractor
# (modules/reseller). The reflective FK sweep used to move them to the keeper
# and count them in the audit row's `rows_updated`, which told staff a
# profile existed; with both customers holding one, the unique customer_id
# turned the merge into a generic 500.


def _profile(db, customer):
    from gdx_dispatch.modules.reseller.models import ResellerProfile

    p = ResellerProfile(id=uuid.uuid4(), customer_id=customer.id,
                        company_name="Brand Co", created_at=datetime.now(UTC))
    db.add(p)
    db.commit()
    return p


def _profile_owners(db):
    return {
        uuid.UUID(r[0]) for r in db.execute(
            text("SELECT CAST(customer_id AS TEXT) FROM reseller_profiles")
        ).all()
    }


def test_both_customers_resellers_is_a_409_not_a_500(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    _profile(db, keep)
    _profile(db, loser)
    job = _job(db, loser)

    with pytest.raises(HTTPException) as exc:
        _merge(db, keep, [loser])

    assert exc.value.status_code == 409
    assert "is a reseller" in exc.value.detail
    assert "Brand Co" not in exc.value.detail, "no profile data in the refusal"
    assert _live(db, loser)
    owner = db.execute(
        text("SELECT CAST(customer_id AS TEXT) FROM jobs WHERE id = :i"), {"i": job.id.hex}
    ).scalar()
    assert uuid.UUID(owner) == loser.id, "nothing may move on a refused merge"
    assert _profile_owners(db) == {keep.id, loser.id}


def test_only_the_loser_a_reseller_is_refused_and_nothing_moves(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    _profile(db, loser)

    with pytest.raises(HTTPException) as exc:
        _merge(db, keep, [loser])

    assert exc.value.status_code == 409
    # Actionable: the way through is to keep the reseller.
    assert "Keep 'Troy example'" in exc.value.detail
    assert _live(db, loser)
    assert _profile_owners(db) == {loser.id}, "the profile must not move"
    assert db.execute(text(
        "SELECT COUNT(*) FROM audit_logs WHERE action = 'merge_customers'"
    )).scalar() == 0


def test_a_loser_with_only_resale_quotes_is_refused_too(db):
    from gdx_dispatch.modules.reseller.models import ResaleQuote

    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    db.add(ResaleQuote(id=uuid.uuid4(), customer_id=loser.id, estimate_id=uuid.uuid4(),
                       created_by=uuid.uuid4(), reference="R-1", markup_pct=10,
                       lines_snapshot={}, created_at=datetime.now(UTC)))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _merge(db, keep, [loser])
    assert exc.value.status_code == 409


def test_a_keeper_only_reseller_merges_and_the_audit_row_never_names_the_tables(db):
    """The falsifier for the skip: a keeper's profile is never moved, but the
    sweep used to visit the table anyway. Neither the response nor the
    persisted audit row may carry a reseller key."""
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    _profile(db, keep)
    _job(db, loser)

    out = _merge(db, keep, [loser])

    assert out.rows_updated.get("jobs.customer_id") == 1
    assert not any("resel" in k or "resale" in k for k in out.rows_updated), out.rows_updated
    details = str(db.execute(text(
        "SELECT details FROM audit_logs WHERE action = 'merge_customers'"
    )).scalar())
    assert "resel" not in details and "resale" not in details, details
    assert _profile_owners(db) == {keep.id}


def test_no_reseller_merge_is_unchanged_and_audit_lacks_the_key(db):
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    out = _merge(db, keep, [loser])
    assert out.rows_updated == {"customers.soft_deleted": 1}
    details = str(db.execute(text(
        "SELECT details FROM audit_logs WHERE action = 'merge_customers'"
    )).scalar())
    assert "reseller_profiles" not in details


def test_the_sweep_skips_the_private_tables_even_past_the_refusal(db, monkeypatch):
    """The skip is its own guard, not a restatement of the 409: with the
    refusal switched off, a loser's profile still stays put and the audit row
    still carries no reseller key."""
    import gdx_dispatch.routers.customers as customers_mod

    monkeypatch.setattr(customers_mod, "_resellers_among", lambda *a, **k: ([], False))
    keep = _customer(db, "Troy Example")
    loser = _customer(db, "Troy example")
    _profile(db, loser)

    out = _merge(db, keep, [loser])

    assert not any("resel" in k for k in out.rows_updated), out.rows_updated
    details = str(db.execute(text(
        "SELECT details FROM audit_logs WHERE action = 'merge_customers'"
    )).scalar())
    assert "reseller_profiles" not in details, details
    assert _profile_owners(db) == {loser.id}
