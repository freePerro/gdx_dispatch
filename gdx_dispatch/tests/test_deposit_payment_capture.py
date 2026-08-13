"""Recording a downpayment that was ALREADY received (2026-08-13).

Doug: "a customer hands a form of payment for the downpayment, so you go back
to the office and hit Accept on the estimate and it asks for a downpayment —
but it only creates the invoice and a link to pay. No way to record a payment
that has already been received."

The damage that omission causes is pinned by
test_unrecorded_deposit_is_voided_and_customer_is_overbilled: building the
final invoice VOIDS a wholly-unpaid deposit and adds no netting line, so a
customer who handed over a $500 check gets billed the full job total — and
record_payment then refuses the late correction because the invoice is void.

Scope, stated plainly: these all pass on main. The money math was never broken;
the defect was that no UI could reach it, so in the field the paid-deposit path
never ran. This file pins the backend contract the new capture forms depend on
(and the two gaps they do NOT close — see the double-tap and orphan tests). The
fix itself is proven by the frontend specs.

Every test drives the real endpoints rather than the ORM, because reachability
is the whole subject.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.billing_predicates import job_billed_exists
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.deposits import create_deposit_invoice
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    PaymentCreateIn,
    create_invoice,
    record_payment,
)

USER = {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin", "sub": "user-1"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        InvoiceAdjustment.__table__,
        JobPartNeeded.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_customer(db) -> Customer:
    cust = Customer(id=uuid4(), name="Cash Payer", company_id="tenant-1")
    db.add(cust)
    db.commit()
    return cust


def _seed_job(db, customer) -> Job:
    job = Job(
        id=uuid4(),
        customer_id=customer.id,
        title="Install",
        lifecycle_stage="scheduled",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_estimate(db, customer, *, total=1000.0, job=None) -> Estimate:
    est = Estimate(
        id=uuid4(),
        job_id=(job.id if job is not None else None),
        customer_id=customer.id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        label="16x7 door",
        proposal_mode=False,
        total=Decimal(str(total)),
        status="accepted",
        public_token=uuid4().hex,
        company_id="tenant-1",
    )
    db.add(est)
    db.flush()
    db.add(EstimateLine(
        estimate_id=est.id,
        description="Door + install",
        quantity=1,
        unit_price=Decimal(str(total)),
        line_total=Decimal(str(total)),
        sort_order=1,
        company_id="tenant-1",
    ))
    db.commit()
    db.refresh(est)
    return est


def _deposit(db, est, amount=500.0) -> Invoice:
    return create_deposit_invoice(
        db, estimate=est, amount=amount, tenant_id="tenant-1",
        actor="user-1", source="test",
    )


def _record(db, invoice, **kw):
    """Drive the real endpoint — the whole defect was reachability."""
    payload = PaymentCreateIn(**{
        "amount": kw.pop("amount", 500.0),
        "method": kw.pop("method", "Check"),
        "date": kw.pop("date", date.today()),
        **kw,
    })
    return record_payment(invoice.id, payload, USER, db)


def _final(db, est, job, *, force=False):
    payload = InvoiceCreateIn(
        job_id=job.id, estimate_id=est.id, customer_id=est.customer_id, force=force,
    )
    return create_invoice(payload, USER, db)


def _lines_of(db, created):
    """Live lines of an invoice the create endpoint just returned.

    The endpoint answers with a dict whose `id` is a STRING; comparing that
    straight against the Uuid column blows up inside SQLAlchemy's bind
    processor, so coerce first.
    """
    inv_id = UUID(created["id"]) if isinstance(created, dict) else created.id
    return db.execute(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == inv_id, InvoiceLine.deleted_at.is_(None)
        )
    ).scalars().all()


def _live_payments(db, invoice_id):
    return db.execute(
        select(Payment).where(
            Payment.invoice_id == invoice_id, Payment.voided_at.is_(None)
        )
    ).scalars().all()


# ---------------------------------------------------------------------------
# 1. The capture itself
# ---------------------------------------------------------------------------

def test_cash_on_fresh_deposit_settles_it_without_billing_the_job(db):
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, job=job)
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=500.0, method="Cash")
    db.refresh(dep)

    assert float(dep.balance_due) == 0.0
    assert dep.status == "paid"
    # A settled DEPOSIT must never read as "the job is billed", or the
    # Build-invoice CTA disappears and the job can never be final-billed.
    # job_billed_exists is a correlated SQL predicate, not a function call.
    billed = db.execute(select(Job.id).where(job_billed_exists())).scalars().all()
    assert job.id not in billed


def test_partial_deposit_payment_is_allowed(db):
    """The customer hands over less than the full deposit — record what
    actually changed hands, not a convenient round number."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=200.0, method="Cash")
    db.refresh(dep)

    assert float(dep.balance_due) == 300.0
    assert dep.status != "paid"


def test_backdated_payment_is_accepted(db):
    """The money changed hands in the field days before the office reached a
    desktop. If the date silently stamped "today", every field-collected
    deposit would land on the wrong day and, at month-end, the wrong month."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)
    three_days_ago = date.today() - timedelta(days=3)

    _record(db, dep, amount=500.0, method="Check", reference="1042", date=three_days_ago)

    (paid,) = _live_payments(db, dep.id)
    assert paid.payment_date == three_days_ago
    assert paid.reference == "1042"


def test_forward_dated_payment_is_refused(db):
    """A post-dated check is not received cash."""
    with pytest.raises(Exception):
        PaymentCreateIn(amount=100.0, method="Check", date=date.today() + timedelta(days=1))


# ---------------------------------------------------------------------------
# 2. The money bug this feature exists to prevent
# ---------------------------------------------------------------------------

def test_deposit_payment_nets_into_final_invoice(db):
    """With the deposit recorded, the final invoice nets it.

    Honest about what this proves: it passes on main too. The netting math was
    never broken — the defect was that no UI could record the payment, so in
    the field this path never ran. This pins the behaviour the new capture
    forms now make reachable; the frontend specs are where the fix is proven.
    """
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=500.0, method="Check", reference="1042")
    final = _final(db, est, job)

    lines = _lines_of(db, final)
    netting = [ln for ln in lines if float(ln.line_total) < 0]
    assert len(netting) == 1, "the paid deposit must net onto the final invoice"
    assert float(netting[0].line_total) == -500.0
    assert "deposit" in (netting[0].description or "").lower()

    db.refresh(dep)
    assert dep.status != "void", "a PAID deposit must never be voided"


def test_unrecorded_deposit_is_voided_and_customer_is_overbilled(db):
    """The bug, pinned. This is what happens today when the check has nowhere
    to go: the deposit is voided, no netting line exists, the final bills the
    full total — and the late correction is then blocked outright.

    Asserting the broken behaviour on purpose: it is CORRECT for a genuinely
    abandoned deposit, so the fix is upstream (let the money be recorded), not
    here. If someone ever "fixes" the void branch, this test should make them
    stop and read.
    """
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _deposit(db, est, amount=500.0)

    final = _final(db, est, job)  # nobody recorded the check

    lines = _lines_of(db, final)
    assert not [ln for ln in lines if float(ln.line_total) < 0], "no netting line"

    db.refresh(dep)
    assert dep.status == "void"

    # ...and the office can no longer record the check they found later.
    with pytest.raises(HTTPException) as exc:
        _record(db, dep, amount=500.0, method="Check")
    assert exc.value.status_code == 409
    assert "void" in str(exc.value.detail).lower()


def test_partially_paid_deposit_supersedes_remainder(db):
    """A mistyped LOW amount is the quiet version of the same damage: the
    shortfall is credit-memo'd as superseded rather than billed, so the
    customer is over-billed while every screen reads settled. This is why the
    capture form prefills the amount instead of making someone retype it."""
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=200.0, method="Cash")  # fat-fingered
    _final(db, est, job)

    memo = db.execute(
        select(InvoiceAdjustment).where(
            InvoiceAdjustment.invoice_id == dep.id,
            InvoiceAdjustment.kind == "credit_memo",
        )
    ).scalars().first()
    assert memo is not None
    assert float(memo.amount) == 300.0
    assert "superseded" in (memo.reason or "").lower()


# ---------------------------------------------------------------------------
# 3. Guards the capture form relies on
# ---------------------------------------------------------------------------

def test_duplicate_check_number_is_refused(db):
    """The check # in `reference` is what dedupes a check payment — migration
    056's partial unique index. The cash confirmation exists precisely because
    cash has no equivalent."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=250.0, method="Check", reference="1042")
    with pytest.raises(HTTPException) as exc:
        _record(db, dep, amount=250.0, method="Check", reference="1042")
    assert exc.value.status_code == 409
    assert len(_live_payments(db, dep.id)) == 1


def test_double_tap_cash_is_refused(db):
    """Cash carries no reference, so neither the exact-reference guard nor
    migration 056's partial index can see it — both are `reference IS NOT NULL`.

    That gap stopped being theoretical when the field surfaces moved to the
    offline queue: a request that errors AFTER the server commits is replayed
    unattended, and the client-side cash confirmation does not run on a replay.
    The Idempotency-Key middleware that would otherwise catch it never runs in
    production. Hence the reference-less window in record_payment.
    """
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=250.0, method="Cash")
    with pytest.raises(HTTPException) as exc:
        _record(db, dep, amount=250.0, method="Cash")

    assert exc.value.status_code == 409
    assert "duplicate" in str(exc.value.detail).lower()
    assert len(_live_payments(db, dep.id)) == 1


def test_the_dedupe_window_does_not_block_a_different_amount(db):
    """Two genuinely different cash amounts are two payments, not a replay."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=200.0, method="Cash")
    _record(db, dep, amount=300.0, method="Cash")

    assert len(_live_payments(db, dep.id)) == 2


def test_the_dedupe_window_does_not_block_a_referenced_repeat(db):
    """A reference is the operator's claim that this is a distinct payment —
    the escape hatch the 409 message points at."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=250.0, method="Cash")
    _record(db, dep, amount=250.0, method="Cash", reference="second envelope")

    assert len(_live_payments(db, dep.id)) == 2


def test_the_dedupe_window_distinguishes_method(db):
    """$250 cash and $250 check on one invoice are two different payments —
    someone paid part in cash and part by check. Dropping the method predicate
    would silently swallow the second."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=250.0, method="Cash")
    _record(db, dep, amount=250.0, method="Check")  # no check # — still distinct

    assert len(_live_payments(db, dep.id)) == 2


def test_a_voided_payment_does_not_block_re_recording_the_same_amount(db):
    """Void-then-re-record is the normal way to correct a mistake. If the
    window counted voided rows, the corrected payment would be refused and the
    operator would be stuck — with the original already voided."""
    from gdx_dispatch.routers.invoices import void_payment

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=250.0, method="Cash")
    wrong = _live_payments(db, dep.id)[0]
    void_payment(dep.id, wrong.id, _=USER, db=db)

    _record(db, dep, amount=250.0, method="Cash")  # the corrected entry
    assert len(_live_payments(db, dep.id)) == 1


def test_the_dedupe_window_expires(db):
    """It is a replay window, not a permanent ban on repeating an amount."""
    from gdx_dispatch.routers import invoices as inv_mod

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=100.0, method="Cash")
    # Age the first payment past the window rather than sleeping.
    first = _live_payments(db, dep.id)[0]
    first.created_at = datetime.now(UTC) - timedelta(
        seconds=inv_mod._CASHLIKE_DEDUPE_SECONDS + 5
    )
    db.commit()

    _record(db, dep, amount=100.0, method="Cash")
    assert len(_live_payments(db, dep.id)) == 2


def test_method_is_stored_lowercase_whatever_the_ui_sends(db):
    """The office UI sends 'Cash', mobile sends 'cash'. record_payment
    lowercases at the write boundary so the column has one spelling."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, job=_seed_job(db, cust))
    dep = _deposit(db, est, amount=500.0)

    _record(db, dep, amount=100.0, method="Check", reference="A")
    _record(db, dep, amount=100.0, method="check", reference="B")  # refs differ

    methods = {p.method for p in _live_payments(db, dep.id)}
    assert methods == {"check"}


def test_payment_on_superseded_deposit_points_at_the_final(db):
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _deposit(db, est, amount=500.0)
    _record(db, dep, amount=200.0, method="Cash")
    _final(db, est, job)

    with pytest.raises(HTTPException) as exc:
        _record(db, dep, amount=300.0, method="Check", reference="late")
    assert exc.value.status_code == 409
    assert "final invoice" in str(exc.value.detail).lower()


# ---------------------------------------------------------------------------
# 4. The orphan case — deposit minted before the job existed
# ---------------------------------------------------------------------------

def test_orphan_deposit_payment_nets_once_the_estimate_gains_a_job(db):
    """A mobile accept mints the deposit with job_id NULL. The netting match is
    on job_id OR estimate_id, so as long as the final carries estimate_id the
    money still lands."""
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0, job=None)  # no job yet
    dep = _deposit(db, est, amount=500.0)
    assert dep.job_id is None

    _record(db, dep, amount=500.0, method="Cash")

    job = _seed_job(db, cust)
    est.job_id = job.id
    db.commit()
    final = _final(db, est, job)

    lines = _lines_of(db, final)
    netting = [ln for ln in lines if float(ln.line_total) < 0]
    assert len(netting) == 1
    assert float(netting[0].line_total) == -500.0


def test_orphan_deposit_is_missed_when_the_final_has_no_estimate_link(db):
    """Known gap (plan D6), pinned so it is a decision and not a surprise.

    If the job is created by a non-estimate path and the final invoice carries
    only job_id, an orphan deposit matches on neither key — the money is
    recorded but never nets, and the customer is over-billed. The mitigation
    that ships today is capturing at accept time, where the estimate link
    always exists.
    """
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0, job=None)
    dep = _deposit(db, est, amount=500.0)
    _record(db, dep, amount=500.0, method="Cash")

    job = _seed_job(db, cust)  # created independently; estimate never linked
    payload = InvoiceCreateIn(job_id=job.id, customer_id=cust.id)
    final = create_invoice(payload, USER, db)

    lines = _lines_of(db, final)
    assert not [ln for ln in lines if float(ln.line_total) < 0], (
        "documenting D6: an unlinked final cannot find the orphan deposit"
    )
    db.refresh(dep)
    assert float(dep.balance_due) == 0.0, "the money IS recorded — just not netted"
