"""055_job_not_billable — the Ready-for-Billing dismiss verb (2026-08-04).

RFB had exactly two exits: Create Invoice, or sit in the queue forever.
Warranty/goodwill/internal jobs and outright mistakes had no "this will
never be invoiced" verb (the leaked-parts card got one in PR4; the job-level
queue never did). These tests pin:

1. job_billing_resolved() — billed OR marked not billable, and nothing else.
2. Mark → the job leaves /api/jobs/ready-for-billing AND the summary count
   (the two must keep agreeing); unmark → it returns.
3. The mark demands a non-whitespace reason (staff-decline rule,
   Doug 2026-07-30) and refuses already-billed jobs (409 — void the
   invoice instead, a mark on a billed job would be a lie on the record).
4. Unmark is idempotent — clearing an unmarked job is a 200 no-op.
5. invoice_now stops nagging a not-billable job.
6. Both verbs land audit events.

Permission gates (invoices.write) resolve only inside the FastAPI app, so
direct-call tests here exercise the handlers, not the gate — same contract
as test_billing_predicates_pr2.py.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.billing_predicates import job_billing_resolved
from gdx_dispatch.core.recommendations import RecommendationEngine
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job, Payment
from gdx_dispatch.routers.invoices import billing_summary
from gdx_dispatch.routers.jobs import (
    NotBillablePayload,
    mark_job_not_billable,
    ready_for_billing,
    unmark_job_not_billable,
)


@pytest.fixture
def tenant_db_session():
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
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)

    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _current_user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": "tenant-1"}, request_id="req-1"),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-request-id": "req-1"},
    )


def _seed_job(db, *, stage: str = "completed", title: str = "Job") -> Job:
    job = Job(
        customer_id=uuid4(),
        title=title,
        description="t",
        lifecycle_stage=stage,
        dispatch_status="done",
        billing_status="unbilled",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_invoice(db, job, *, status: str = "sent", total: float = 500.0) -> Invoice:
    inv = Invoice(
        company_id="tenant-1",
        customer_id=job.customer_id,
        job_id=job.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal(str(total)),
        tax_amount=Decimal("0"),
        total=Decimal(str(total)),
        balance_due=Decimal(str(total)),
        status=status,
        invoice_date=date.today(),
        due_date=date.today(),
        public_token=uuid4().hex,
        locked=False,
    )
    db.add(inv)
    db.commit()
    return inv


def _mark(db, job, reason: str = "warranty — free rework"):
    return mark_job_not_billable(
        job_id=str(job.id),
        payload=NotBillablePayload(reason=reason),
        request=_request(),
        current_user=_current_user(),
        db=db,
    )


def _unmark(db, job):
    return unmark_job_not_billable(
        job_id=str(job.id),
        request=_request(),
        current_user=_current_user(),
        db=db,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def test_resolved_predicate_matrix(tenant_db_session):
    db = tenant_db_session
    plain = _seed_job(db, title="plain unbilled")
    billed = _seed_job(db, title="billed")
    _seed_invoice(db, billed)
    marked = _seed_job(db, title="not billable")
    marked.not_billable_at = datetime.now(UTC)
    marked.not_billable_reason = "warranty"
    db.commit()

    resolved = {
        str(r) for r in db.execute(select(Job.id).where(job_billing_resolved())).scalars()
    }
    assert str(plain.id) not in resolved
    assert str(billed.id) in resolved
    assert str(marked.id) in resolved


def test_mark_removes_from_rfb_and_summary_then_unmark_restores(tenant_db_session):
    db = tenant_db_session
    keep = _seed_job(db, title="still billable")
    dismiss = _seed_job(db, title="warranty job")

    before = {r["id"] for r in ready_for_billing(request=None, current_user=_current_user(), db=db)}
    assert before == {str(keep.id), str(dismiss.id)}
    assert billing_summary(request=None, _=_current_user(), db=db)["ready_for_billing"] == 2

    resp = _mark(db, dismiss)
    assert resp.status_code == 200
    db.refresh(dismiss)
    assert dismiss.not_billable_at is not None
    assert dismiss.not_billable_reason == "warranty — free rework"
    assert dismiss.not_billable_by == "user-1"

    after = {r["id"] for r in ready_for_billing(request=None, current_user=_current_user(), db=db)}
    assert after == {str(keep.id)}
    assert billing_summary(request=None, _=_current_user(), db=db)["ready_for_billing"] == 1

    resp = _unmark(db, dismiss)
    assert resp.status_code == 200
    db.refresh(dismiss)
    assert dismiss.not_billable_at is None
    assert dismiss.not_billable_reason is None
    restored = {r["id"] for r in ready_for_billing(request=None, current_user=_current_user(), db=db)}
    assert restored == {str(keep.id), str(dismiss.id)}


def test_whitespace_reason_rejected(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    resp = _mark(db, job, reason="   ")
    assert resp.status_code == 422
    db.refresh(job)
    assert job.not_billable_at is None


def test_mark_requires_completed_stage(tenant_db_session):
    """The verb belongs to the RFB queue, which contains only completed jobs.
    Marking a job mid-flight would hide the mobile Bill button while the work
    is live — with a require-invoice completion gate that's a deadlock."""
    db = tenant_db_session
    job = _seed_job(db, stage="scheduled")
    resp = _mark(db, job)
    assert resp.status_code == 409
    assert "completed" in _body(resp)["detail"]
    db.refresh(job)
    assert job.not_billable_at is None


def test_mobile_flags_stay_honest(tenant_db_session):
    """`billed` must never lie: a not-billable job has NO invoice, so it ships
    as its own not_billable key and the Bill-button gate reads both."""
    from gdx_dispatch.routers.mobile import _job_is_billed, _job_not_billable

    db = tenant_db_session
    marked = _seed_job(db, title="dismissed")
    _mark(db, marked)
    assert _job_is_billed(db, str(marked.id)) is False
    assert _job_not_billable(db, str(marked.id)) is True

    billed = _seed_job(db, title="invoiced")
    _seed_invoice(db, billed)
    assert _job_is_billed(db, str(billed.id)) is True
    assert _job_not_billable(db, str(billed.id)) is False


def test_mark_billed_job_409(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db, title="already billed")
    _seed_invoice(db, job)
    resp = _mark(db, job)
    assert resp.status_code == 409
    assert "billed" in _body(resp)["detail"]
    db.refresh(job)
    assert job.not_billable_at is None


def test_mark_unknown_and_malformed_job_404(tenant_db_session):
    db = tenant_db_session
    resp = mark_job_not_billable(
        job_id=str(uuid4()),
        payload=NotBillablePayload(reason="x"),
        request=_request(),
        current_user=_current_user(),
        db=db,
    )
    assert resp.status_code == 404
    resp = mark_job_not_billable(
        job_id="not-a-uuid",
        payload=NotBillablePayload(reason="x"),
        request=_request(),
        current_user=_current_user(),
        db=db,
    )
    assert resp.status_code == 404


def test_unmark_is_idempotent(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    resp = _unmark(db, job)  # never marked
    assert resp.status_code == 200
    assert _body(resp)["ok"] is True
    # No audit noise for the no-op: nothing changed, nothing to record.
    events = db.execute(
        select(AuditLog).where(AuditLog.action == "job_not_billable_cleared")
    ).scalars().all()
    assert events == []


def test_invoice_now_not_fired_for_not_billable_job(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db, title="goodwill")
    _mark(db, job, reason="goodwill")
    recs = RecommendationEngine().get_job_recommendations("tenant-1", str(job.id), db)
    assert "invoice_now" not in {r["type"] for r in recs}


def test_audit_events_written(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    _mark(db, job, reason="duplicate entry")
    _unmark(db, job)

    actions = [
        (row.action, row.details)
        for row in db.execute(
            select(AuditLog).where(AuditLog.entity_id == str(job.id)).order_by(AuditLog.created_at)
        ).scalars()
    ]
    assert [a for a, _ in actions] == ["job_marked_not_billable", "job_not_billable_cleared"]
    mark_details = actions[0][1]
    assert mark_details["reason"] == "duplicate entry"
    clear_details = actions[1][1]
    assert clear_details["prior_reason"] == "duplicate entry"
