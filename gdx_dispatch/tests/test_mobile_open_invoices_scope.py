"""GET /api/mobile/invoices/open — the tech-scoped receivables view (2026-08-13).

Why the endpoint exists: the `technician` role has NO invoices permission of any
kind, so `GET /api/invoices` 403s for the field tier and the mobile billing
screen was unreachable for its only intended user. Granting invoices.read_all
would hand every tech the entire receivables book, so instead they get what
their own work produced.

What these tests defend, in order of how badly they'd hurt if wrong:

  1. It does not leak another tech's invoices. (Money privacy.)
  2. It DOES include a deposit whose job_id is NULL but whose estimate points at
     one of the tech's jobs — that is the invoice the tech is most likely to be
     trying to settle, and a job_id-only filter would silently hide it.
  3. Settled and void invoices stay out, so the list is actionable.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    Payment,
)
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.mobile_invoicing import mobile_open_invoices

TENANT = "tenant-1"


class _State:
    tenant = {"id": TENANT}


class _Req:
    state = _State()


MINE = {"user_id": "user-mine", "tenant_id": TENANT, "sub": "user-mine"}
OTHER = {"user_id": "user-other", "tenant_id": TENANT, "sub": "user-other"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__, Customer.__table__, Invoice.__table__,
        InvoiceLine.__table__, Payment.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    # jobs.assigned_to holds a TECHNICIAN id, not a user id — the ownership gate
    # resolves it through technicians.user_id. Getting this backwards is the
    # exact A1 audit bug that made field billing 404 on ~90% of jobs.
    session.execute(TenantBase.metadata.tables["technicians"].insert().values(
        id="tech-mine", user_id="user-mine", company_id=TENANT, name="Mine",
    ))
    session.execute(TenantBase.metadata.tables["technicians"].insert().values(
        id="tech-other", user_id="user-other", company_id=TENANT, name="Other",
    ))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _customer(db):
    c = Customer(id=uuid4(), name="Payer", company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _job(db, customer, tech_id):
    j = Job(
        id=uuid4(), customer_id=customer.id, title="Install",
        lifecycle_stage="scheduled", dispatch_status="assigned",
        billing_status="unbilled", assigned_to=tech_id, company_id=TENANT,
    )
    db.add(j)
    db.commit()
    db.refresh(j)
    return j


def _invoice(db, customer, *, job=None, estimate=None, balance=250.0,
             status="sent", billing_type="standard"):
    inv = Invoice(
        id=uuid4(),
        job_id=(job.id if job is not None else None),
        estimate_id=(estimate.id if estimate is not None else None),
        customer_id=customer.id,
        invoice_number=f"INV-{uuid4().hex[:6]}",
        billing_type=billing_type,
        sequence_number=1,
        subtotal=Decimal(str(balance)),
        tax_amount=0,
        total=Decimal(str(balance)),
        balance_due=Decimal(str(balance)),
        status=status,
        invoice_date=date.today(),
        due_date=date.today(),
        public_token=uuid4().hex,
        company_id=TENANT,
        created_at=datetime.now(UTC),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _estimate(db, customer, *, job=None):
    est = Estimate(
        id=uuid4(),
        job_id=(job.id if job is not None else None),
        customer_id=customer.id,
        estimate_number=f"EST-{uuid4().hex[:6]}",
        label="Door",
        proposal_mode=False,
        total=Decimal("1000"),
        status="accepted",
        public_token=uuid4().hex,
        company_id=TENANT,
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    return est


def _numbers(resp):
    import json

    return {i["invoice_number"] for i in json.loads(resp.body)["invoices"]}


def test_lists_unpaid_invoices_on_my_own_jobs(db):
    cust = _customer(db)
    mine = _invoice(db, cust, job=_job(db, cust, "tech-mine"))
    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == {mine.invoice_number}


def test_does_not_leak_another_techs_invoices(db):
    cust = _customer(db)
    _invoice(db, cust, job=_job(db, cust, "tech-other"))
    # MINE must own at least one job with its own invoice, or the endpoint
    # short-circuits on the empty job list and the scoping SQL never runs —
    # which made an earlier version of this test pass even with the scope
    # clause mutated to `1=1`.
    mine = _invoice(db, cust, job=_job(db, cust, "tech-mine"))

    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == {mine.invoice_number}


def test_includes_a_deposit_with_no_job_when_its_estimate_points_at_my_job(db):
    """The case the endpoint exists for. A deposit minted at estimate
    acceptance carries job_id NULL until the estimate becomes a job — a
    job_id-only filter would hide exactly the invoice the tech is settling."""
    cust = _customer(db)
    job = _job(db, cust, "tech-mine")
    est = _estimate(db, cust, job=job)
    dep = _invoice(db, cust, job=None, estimate=est, billing_type="deposit")

    assert dep.job_id is None
    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == {dep.invoice_number}


def test_does_not_leak_a_deposit_whose_estimate_belongs_to_another_tech(db):
    cust = _customer(db)
    est = _estimate(db, cust, job=_job(db, cust, "tech-other"))
    _invoice(db, cust, job=None, estimate=est, billing_type="deposit")
    mine = _invoice(db, cust, job=_job(db, cust, "tech-mine"))  # see note above

    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == {mine.invoice_number}


def test_a_truly_orphan_deposit_is_not_listed(db):
    """No job anywhere means no ownership signal. Listing it would leak other
    people's money, so the answer for these is capture-at-accept."""
    cust = _customer(db)
    est = _estimate(db, cust, job=None)
    _invoice(db, cust, job=None, estimate=est, billing_type="deposit")
    _job(db, cust, "tech-mine")  # the tech has jobs, just not this estimate's
    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == set()


def test_paid_and_void_invoices_are_excluded(db):
    cust = _customer(db)
    job = _job(db, cust, "tech-mine")
    _invoice(db, cust, job=job, status="paid", balance=0.0)
    _invoice(db, cust, job=job, status="void", balance=100.0)
    live = _invoice(db, cust, job=job, status="sent", balance=100.0)
    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == {live.invoice_number}


def test_drafts_are_excluded_so_the_rfb_gate_cannot_be_bypassed(db):
    """The closeout autodrafts an invoice on the tech's own job. If those were
    listed, a tech could collect against a bill the office has never reviewed —
    and recording the payment zeroes balance_due, which flips the draft to
    "paid" and skips Ready-for-Billing entirely."""
    cust = _customer(db)
    job = _job(db, cust, "tech-mine")
    _invoice(db, cust, job=job, status="draft", balance=400.0)
    issued = _invoice(db, cust, job=job, status="sent", balance=100.0)

    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == {issued.invoice_number}


def test_results_are_ordered_by_due_date_across_job_chunks(db, monkeypatch):
    """The job-id IN-lists are chunked; sorting inside the loop would return an
    order that is only correct WITHIN each chunk.

    The chunk size is patched down to 1 so two jobs land in two chunks. Seeding
    two jobs against the real size of 200 exercised a single chunk, which meant
    deleting the cross-chunk sort entirely left the test green.
    """
    import json

    from gdx_dispatch.routers import mobile_invoicing as mi

    monkeypatch.setattr(mi, "_JOB_ID_CHUNK", 1)

    cust = _customer(db)
    early = _invoice(db, cust, job=_job(db, cust, "tech-mine"), balance=100.0)
    late = _invoice(db, cust, job=_job(db, cust, "tech-mine"), balance=100.0)
    # Number them AGAINST the due-date order: if anything ever sorted by number
    # (or returned chunk order), these would come back reversed.
    early.due_date = date(2026, 1, 1)
    early.invoice_number = "INV-ZZZ999"
    late.due_date = date(2026, 12, 31)
    late.invoice_number = "INV-AAA111"
    db.commit()

    body = json.loads(mobile_open_invoices(_Req(), MINE, db).body)
    assert [i["invoice_number"] for i in body["invoices"]] == [
        early.invoice_number, late.invoice_number,
    ]
    assert body["truncated"] is False


def test_truncation_is_reported_in_the_body(db, monkeypatch):
    """A capped list that looks complete is how "I never saw that invoice"
    starts — the flag has to reach the client, not just the server log."""
    import json

    from gdx_dispatch.routers import mobile_invoicing as mi

    monkeypatch.setattr(mi, "_OPEN_INVOICE_CAP", 2)

    cust = _customer(db)
    job = _job(db, cust, "tech-mine")
    for _ in range(3):
        _invoice(db, cust, job=job, balance=100.0)

    body = json.loads(mobile_open_invoices(_Req(), MINE, db).body)
    assert len(body["invoices"]) == 2
    assert body["truncated"] is True


def test_no_jobs_means_an_empty_list_not_an_error(db):
    assert _numbers(mobile_open_invoices(_Req(), MINE, db)) == set()


def test_unauthenticated_caller_is_refused(db):
    resp = mobile_open_invoices(_Req(), {}, db)
    assert resp.status_code == 401
