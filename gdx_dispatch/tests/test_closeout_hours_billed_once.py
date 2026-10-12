"""A closeout's final-day hours are billed once (GDXA-799, migration 118).

Day rows carry ``billed_invoice_id``; the closeout's own ``hours_worked`` did
not, so every invoice after the first re-offered them. These tests drive the
real route functions and the real builder against an ORM-built SQLite
database and read back what landed: the ``job_closeouts.billed_invoice_id``
stamp, the invoice totals, and the audit rows.
"""
from __future__ import annotations

import os
import threading
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.closeout_billing import (
    AUTODRAFT_ORIGIN,
    autodraft_invoice_for_closeout,
    build_closeout_lines,
    claim_closeout_hours,
    release_untouched_autodraft,
)
from gdx_dispatch.models.tenant_models import Invoice, JobCloseout
from gdx_dispatch.routers.invoices import add_invoice_line, delete_invoice, void_invoice
from gdx_dispatch.tests.conftest import make_fresh_db
from gdx_dispatch.tests.test_labor_billed_through_a_point import (
    ACTOR,
    DAY1,
    DAY2,
    TENANT,
    USER,
    _as_line,
    _audits,
    _closeout,
    _create,
    _job,
    _rates,
    _row,
    _suggest,
)


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _stamp(db, co):
    db.expire_all()
    return db.get(JobCloseout, co.id).billed_invoice_id


def _total(db, inv_id) -> Decimal:
    db.expire_all()
    return Decimal(str(db.get(Invoice, UUID(str(inv_id))).total))


def _bill_from_suggestion(db, job) -> dict:
    _c, body = _suggest(db, job)
    return _create(db, job, [_as_line(ln) for ln in body["labor_lines"]], force=True)


def test_the_ledger_probe_a_second_invoice_does_not_rebill_the_closeout(db):
    # The triage probe: $125 first hour / $100 hourly. A 3 h day row and a
    # 2 h closeout bill $525 on A; then a 1 h row and no new closeout. Before
    # GDXA-799 the suggestion offered 3.0 x $100, re-billing A's 2 h.
    _rates(db, "125", "100")
    job = _job(db)
    _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=2)

    a = _bill_from_suggestion(db, job)
    assert _total(db, a["id"]) == Decimal("525.00")
    assert _stamp(db, co) == UUID(a["id"])
    [claim] = _audits(db, "labor_closeout_claimed", a["id"])
    assert claim.user_id == ACTOR
    assert claim.details["closeout_id"] == str(co.id)

    _row(db, job, at=DAY2, minutes=60)
    _c, body = _suggest(db, job)
    assert sum(Decimal(str(ln["quantity"])) for ln in body["labor_lines"]) == Decimal("1")
    b = _create(db, job, [_as_line(ln) for ln in body["labor_lines"]], force=True)
    assert _total(db, b["id"]) == Decimal("100.00")
    # A keeps the closeout; B claimed nothing of it.
    assert _stamp(db, co) == UUID(a["id"])
    assert _audits(db, "labor_closeout_claimed", b["id"]) == []


def test_voiding_the_invoice_releases_the_stamp_and_the_hours_come_back(db):
    job = _job(db)
    co = _closeout(db, job, hours=2)
    a = _bill_from_suggestion(db, job)
    assert _stamp(db, co) == UUID(a["id"])
    assert _suggest(db, job)[1]["labor_lines"] == []

    void_invoice(invoice_id=UUID(a["id"]), _=USER, db=db)
    assert _stamp(db, co) is None
    [audit] = _audits(db, "labor_closeout_released", a["id"])
    assert audit.user_id == ACTOR
    assert audit.details["why"] == "invoice_voided"
    assert audit.details["released_closeouts"] == 1

    # The void bills nothing, so the 2 h are offered — and claimed — again.
    b = _bill_from_suggestion(db, job)
    assert _total(db, b["id"]) == Decimal("200.00")
    assert _stamp(db, co) == UUID(b["id"])


def test_deleting_the_invoice_releases_the_stamp(db):
    job = _job(db)
    co = _closeout(db, job, hours=1.5)
    a = _bill_from_suggestion(db, job)
    delete_invoice(invoice_id=UUID(a["id"]), current_user=USER, db=db)
    assert _stamp(db, co) is None
    [audit] = _audits(db, "labor_closeout_released", a["id"])
    assert audit.details["why"] == "invoice_deleted"


def test_a_stamp_naming_a_void_invoice_holds_nothing(db):
    # A void that predates the release (or any path that missed it) must not
    # strand the hours: the dead holder is free to the next claim.
    job = _job(db)
    co = _closeout(db, job, hours=2)
    a = _bill_from_suggestion(db, job)
    db.expire_all()
    db.get(Invoice, UUID(a["id"])).status = "void"
    db.commit()
    assert _stamp(db, co) == UUID(a["id"])

    b = _bill_from_suggestion(db, job)
    assert _total(db, b["id"]) == Decimal("200.00")
    assert _stamp(db, co) == UUID(b["id"])


def test_a_second_suggestion_line_on_the_holding_invoice_is_refused(db):
    # No day rows: before GDXA-799 this added the closeout's hours twice to
    # the same invoice without a word.
    job = _job(db)
    co = _closeout(db, job, hours=2)
    a = _bill_from_suggestion(db, job)
    _c, body = _suggest(db, job, invoice_id=a["id"])
    [again] = body["labor_lines"]  # its own stamp counts as unbilled for it
    with pytest.raises(HTTPException) as exc:
        add_invoice_line(invoice_id=UUID(a["id"]), payload=_as_line(again), current_user=USER, db=db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "labor_already_on_invoice"
    assert _stamp(db, co) == UUID(a["id"])
    assert _total(db, a["id"]) == Decimal("200.00")


def test_the_builder_stamps_and_a_second_build_leaves_the_hours_out(db):
    # The mobile truck path and the autodraft share build_closeout_lines.
    job = _job(db)
    co = _closeout(db, job, hours=2)
    first = autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co)
    db.commit()
    assert first is not None and first.origin == AUTODRAFT_ORIGIN
    assert _stamp(db, co) == first.id
    [claim] = _audits(db, "labor_closeout_claimed", first.id)
    assert claim.user_id == co.closed_by_user_id

    second = Invoice(job_id=job.id, customer_id=job.customer_id, invoice_number="INV-900002",
                     status="draft", subtotal=0, tax_amount=0, total=0, balance_due=0,
                     public_token="t2", locked=False, company_id=TENANT)
    db.add(second)
    db.flush()
    added, total, _t = build_closeout_lines(db, tenant_id=TENANT, invoice=second, closeout=co,
                                            job_type=job.job_type, job_id=str(job.id), actor=ACTOR)
    db.commit()
    assert (added, total) == (0, Decimal("0"))
    assert _stamp(db, co) == first.id


def test_an_untouched_autodraft_release_gives_the_hours_back(db):
    job = _job(db)
    co = _closeout(db, job, hours=2)
    draft = autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co)
    db.commit()
    assert _stamp(db, co) == draft.id

    reused = release_untouched_autodraft(db, job=job, actor=ACTOR)
    db.commit()
    assert reused is not None and reused.id == draft.id
    assert _stamp(db, co) is None
    [audit] = _audits(db, "labor_closeout_released", draft.id)
    assert (audit.user_id, audit.details["why"]) == (ACTOR, "autodraft_rebuilt")


def test_a_closeout_with_no_hours_is_never_stamped(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=60)
    co = _closeout(db, job, hours=0)
    a = _bill_from_suggestion(db, job)
    assert _total(db, a["id"]) == Decimal("100.00")
    assert _stamp(db, co) is None
    assert _audits(db, "labor_closeout_claimed", a["id"]) == []



def test_a_suggestion_line_that_loses_the_claim_is_refused(db, monkeypatch):
    # The holder read saw the hours free; another invoice committed its claim
    # before this one's UPDATE ran (the Postgres race below). The line was
    # built with the hours on it, so it must not land unstamped.
    import gdx_dispatch.core.closeout_billing as cb

    job = _job(db)
    co = _closeout(db, job, hours=2)
    _c, body = _suggest(db, job)
    monkeypatch.setattr(cb, "claim_closeout_hours", lambda *_a, **_kw: False)
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [_as_line(ln) for ln in body["labor_lines"]], force=True)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "labor_already_billed"
    assert _stamp(db, co) is None
    db.expire_all()
    assert db.query(Invoice).filter(Invoice.job_id == job.id).count() == 0

# ── a race of two claims ─────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.mark.skipif("postgresql" not in _URL,
                    reason="the claim race needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL")
def test_two_concurrent_closeout_claims_on_postgres_one_wins(monkeypatch):
    """Session A inserts its invoice, claims and holds its transaction open;
    session B's claim blocks on the row lock and, once A commits, sees A's
    invoice live and claims nothing. Without the lock B's NOT IN read a
    snapshot without A's invoice and overwrote the stamp."""
    import gdx_dispatch.core.audit as audit

    monkeypatch.setattr(audit, "log_audit_event_sync", lambda **_kw: None)
    eng = create_engine(_URL, future=True)
    schema = "closeout_claim_race_test"  # a constant: the statements below name it literally
    cid, job_id = uuid4(), uuid4()
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(
            f"CREATE TABLE {schema}.invoices (id uuid PRIMARY KEY, deleted_at timestamptz, status text)"
        )
        c.exec_driver_sql(
            f"CREATE TABLE {schema}.job_closeouts (id uuid PRIMARY KEY, billed_invoice_id uuid, "
            "updated_at timestamptz)"
        )
        c.exec_driver_sql(
            "INSERT INTO closeout_claim_race_test.job_closeouts (id) VALUES (%s)", (str(cid),))
    scoped = eng.execution_options(schema_translate_map={None: schema})
    Session = sessionmaker(bind=scoped)
    a, b = Session(), Session()
    closeout = SimpleNamespace(id=cid, job_id=job_id, hours_worked=Decimal("2"))

    def _invoice(session):
        inv = SimpleNamespace(id=uuid4(), company_id=None, invoice_number=None, job_id=job_id)
        session.connection().exec_driver_sql(
            "INSERT INTO closeout_claim_race_test.invoices (id, status) VALUES (%s, 'draft')",
            (str(inv.id),))
        return inv

    try:
        inv_a = _invoice(a)
        assert claim_closeout_hours(a, inv_a, closeout, actor=ACTOR)  # A holds the row lock
        inv_b = _invoice(b)
        result: dict = {}

        def _b():
            result["b"] = claim_closeout_hours(b, inv_b, closeout, actor=ACTOR)
            b.commit()

        t = threading.Thread(target=_b)
        t.start()
        t.join(timeout=1.0)
        assert t.is_alive(), "B must wait on A's row lock"
        a.commit()
        t.join(timeout=10)
        assert result == {"b": False}
        with eng.connect() as c:
            assert c.exec_driver_sql(
                "SELECT billed_invoice_id FROM closeout_claim_race_test.job_closeouts"
            ).scalar() == inv_a.id
    finally:
        a.close()
        b.close()
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
