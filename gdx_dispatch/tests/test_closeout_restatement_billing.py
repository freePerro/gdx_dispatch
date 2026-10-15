"""A re-closeout of a still-completed job restates its hours (GDXA-890).

Doug, 2026-10-13: while the job stays completed, a second closeout is a
restatement of the same work, so the next invoice offers only the hours above
what was already billed — 2 h billed and re-closed at 3 h bills 3 h in all,
never 5 h. After an explicit uncomplete, a new closeout is new work and bills
its own hours (the reopen tests in test_labor_billed_through_a_point.py and
test_day_close_billing.py pin that side with hand-built rows).

Every test drives the real routes — ``closeout_job``, ``uncomplete_job``, the
billing suggestion, ``create_invoice``, ``void_invoice`` — against an ORM-built
SQLite database and reads back the closeout rows, invoice lines and audit rows.
"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.closeout_billing import closeout_hours_billed_before
from gdx_dispatch.core.closeouts import get_current_closeout
from gdx_dispatch.core.job_taxonomy import SERVICE_CALL
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job, JobCloseout, TimeEntry
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    create_invoice,
    void_invoice,
)
from gdx_dispatch.routers.jobs import (
    CloseoutPayload,
    StateOverridePayload,
    closeout_billing_suggestion,
    closeout_job,
    uncomplete_job,
)
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-restate"
ACTOR = "00000000-0000-0000-0000-0000000000c1"
USER = {"user_id": ACTOR, "tenant_id": TENANT, "role": "admin"}


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(PricingSettings(service_call_first_hour_price=Decimal("125"),
                                service_call_hourly_rate=Decimal("100")))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _job(db) -> Job:
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    db.flush()
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Spring",
              description="", status="In Progress", priority="Normal", job_type=SERVICE_CALL,
              lifecycle_stage="in_progress", dispatch_status="on_site", billing_status="unbilled",
              assigned_to=ACTOR, is_demo=False, is_return_visit=False)
    db.add(job)
    db.commit()
    return job


def _close(db, job, hours) -> dict:
    resp = closeout_job(
        payload=CloseoutPayload(parts=[], hours=hours, no_parts_used=True),
        job_id=str(job.id), request=_request(), current_user=USER, db=db,
    )
    assert resp.status_code == 201, resp.body
    return json.loads(resp.body)


def _uncomplete(db, job) -> None:
    resp = uncomplete_job(
        payload=StateOverridePayload(reason="came back for the opener"),
        job_id=str(job.id), request=_request(), current_user=USER, db=db,
    )
    assert resp.status_code == 200, resp.body


def _suggest(db, job) -> dict:
    resp = closeout_billing_suggestion(
        job_id=str(job.id), request=SimpleNamespace(headers={}, client=None, state=SimpleNamespace()),
        invoice_id=None, current_user=USER, db=db,
    )
    assert resp.status_code == 200, resp.body
    return json.loads(resp.body)


def _as_line(s: dict) -> InvoiceLineCreateIn:
    return InvoiceLineCreateIn(
        description=s["description"], quantity=Decimal(str(s["quantity"])),
        unit_price=s["unit_price"], taxable=False, category="Labor", labor_source="attested",
        estimated_man_hours=s.get("estimated_man_hours"), time_entry_ids=s.get("time_entry_ids"),
        closeout_id=s.get("closeout_id"),
    )


def _create(db, job, lines) -> dict:
    return create_invoice(
        payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id, line_items=lines, force=True),
        _=USER, db=db,
    )


def _send(db, invoice_id) -> None:
    """A human advanced the invoice: the machine may no longer rebuild it."""
    inv = db.get(Invoice, UUID(str(invoice_id)))
    inv.status = "sent"
    inv.sent_at = _dt.datetime.now(_dt.UTC)
    db.commit()


def _labor_hours(db, job) -> Decimal:
    """Labor quantity on every live, non-void invoice of the job."""
    db.expire_all()
    rows = db.execute(
        select(InvoiceLine.quantity)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            Invoice.job_id == job.id,
            Invoice.deleted_at.is_(None),
            Invoice.status != "void",
            InvoiceLine.deleted_at.is_(None),
            InvoiceLine.category == "Labor",
        )
    ).scalars().all()
    return sum((Decimal(str(q)) for q in rows), Decimal(0))


def _billed_first(db, job, hours) -> str:
    """Close out at ``hours``; the closeout's draft goes out to the customer."""
    first = _close(db, job, hours)
    assert first["autodraft_invoice_id"], first
    _send(db, first["autodraft_invoice_id"])
    return first["autodraft_invoice_id"]


def test_a_restatement_of_a_completed_job_offers_only_the_delta(db):
    job = _job(db)
    _billed_first(db, job, 2)
    assert _labor_hours(db, job) == Decimal("2")

    out = _close(db, job, 3)  # the job is still completed: a restatement
    co = get_current_closeout(db, job.id)
    assert co.is_restatement is True
    assert out["is_restatement"] is True
    assert out["man_hours_billed_before"] == 2.0
    assert out["man_hours_billed_above_attested"] == 0.0

    body = _suggest(db, job)
    assert body["closeout"]["man_hours_billed_before"] == 2.0
    [line] = body["labor_lines"]
    # The first hour went out on the first invoice: 1 h at the hourly rate.
    assert (line["quantity"], line["unit_price"], line["closeout_id"]) == (1.0, 100.0, str(co.id))
    _create(db, job, [_as_line(line)])
    assert _labor_hours(db, job) == Decimal("3"), "3 h job billed 3 h, never 5 h"

    # Billed in full now: nothing more to offer.
    assert _suggest(db, job)["labor_lines"] == []


def test_a_restatement_below_what_was_billed_bills_nothing_and_says_so(db):
    job = _job(db)
    _billed_first(db, job, 3)

    out = _close(db, job, 2)
    assert out["man_hours_billed_before"] == 3.0
    assert out["man_hours_billed_above_attested"] == 1.0
    body = _suggest(db, job)
    assert body["labor_lines"] == [], "never a negative line"
    assert body["closeout"]["man_hours_billed_above_attested"] == 1.0

    [audit] = db.execute(
        select(AuditLog).where(AuditLog.action == "job_closeout_superseded")
    ).scalars().all()
    assert audit.user_id == ACTOR
    assert audit.details["is_restatement"] is True
    assert Decimal(audit.details["man_hours_billed_above_attested"]) == Decimal("1")
    assert _labor_hours(db, job) == Decimal("3"), "the billed invoice is left as it was"

    # A line that names the restated closeout anyway is stale: nothing to bill.
    co = get_current_closeout(db, job.id)
    stale = InvoiceLineCreateIn(description="Service labor", quantity=Decimal("2"), unit_price=100,
                                taxable=False, category="Labor", labor_source="attested",
                                closeout_id=str(co.id))
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [stale])
    assert exc.value.status_code == 409


def test_after_an_uncomplete_a_new_closeout_bills_its_own_hours(db):
    job = _job(db)
    _billed_first(db, job, 2)

    _uncomplete(db, job)
    out = _close(db, job, 1)
    co = get_current_closeout(db, job.id)
    assert co.is_restatement is False
    assert out["man_hours_billed_before"] == 0.0

    [line] = _suggest(db, job)["labor_lines"]
    assert (line["quantity"], line["unit_price"], line["closeout_id"]) == (1.0, 100.0, str(co.id))
    _create(db, job, [_as_line(line)])
    assert _labor_hours(db, job) == Decimal("3"), "new work after the reopen: 2 h + 1 h"


def test_an_untouched_draft_is_rebuilt_at_the_restated_total(db):
    job = _job(db)
    first = _close(db, job, 2)
    second = _close(db, job, 3)
    assert second["autodraft_invoice_id"] == first["autodraft_invoice_id"]
    assert second["man_hours_billed_before"] == 0.0
    assert _labor_hours(db, job) == Decimal("3")
    assert _suggest(db, job)["labor_lines"] == []


def test_voiding_the_first_invoice_gives_the_whole_restated_total_back(db):
    job = _job(db)
    first = _billed_first(db, job, 2)
    _close(db, job, 3)
    void_invoice(invoice_id=UUID(first), _=USER, db=db)

    body = _suggest(db, job)
    assert body["closeout"]["man_hours_billed_before"] == 0.0
    assert sum(ln["quantity"] for ln in body["labor_lines"]) == 3.0


def test_a_run_of_restatements_has_billed_the_sum_of_its_held_claims(db):
    """2 h billed, restated 3 h and the delta billed, restated again at 2.5 h:
    the run has billed 2 h + 1 h, so the third offers nothing."""
    job = _job(db)
    _billed_first(db, job, 2)
    _close(db, job, 3)
    [line] = _suggest(db, job)["labor_lines"]
    _create(db, job, [_as_line(line)])

    db.expire_all()
    assert [c.man_hours_billed for c in db.execute(
        select(JobCloseout).where(JobCloseout.billed_invoice_id.is_not(None))
        .order_by(JobCloseout.closed_at)).scalars()] == [Decimal("2.00"), Decimal("1.00")]

    out = _close(db, job, 2.5)
    assert out["man_hours_billed_before"] == 3.0
    assert out["man_hours_billed_above_attested"] == 0.5
    assert _suggest(db, job)["labor_lines"] == []
    assert _labor_hours(db, job) == Decimal("3")


def test_voiding_the_first_invoice_after_the_delta_is_billed_strands_nothing(db):
    """2 h billed, restated 3 h and the 1 h delta billed, then the FIRST
    invoice is voided: 2 attested hours are on no live invoice. The screen
    says so, and restating the closeout offers exactly those 2 h."""
    job = _job(db)
    first = _billed_first(db, job, 2)
    _close(db, job, 3)
    [line] = _suggest(db, job)["labor_lines"]
    _create(db, job, [_as_line(line)])
    void_invoice(invoice_id=UUID(first), _=USER, db=db)
    assert _labor_hours(db, job) == Decimal("1")

    body = _suggest(db, job)
    assert body["labor_lines"] == []
    assert body["closeout"]["man_hours_left_unbilled"] == 2.0

    out = _close(db, job, 3)
    assert out["man_hours_billed_before"] == 1.0, "only the live delta invoice is netted"
    lines = _suggest(db, job)["labor_lines"]
    assert sum(ln["quantity"] for ln in lines) == 2.0
    _create(db, job, [_as_line(ln) for ln in lines])
    assert _labor_hours(db, job) == Decimal("3")
    assert _suggest(db, job)["closeout"]["man_hours_left_unbilled"] == 0.0


def test_a_delta_suggestion_saved_after_the_first_invoice_is_voided_is_stale(db):
    """The office reads the 1 h delta, the first invoice is voided, then the
    save: 3 h are now left, so the 1 h line would record 3 h as billed. 409."""
    job = _job(db)
    first = _billed_first(db, job, 2)
    _close(db, job, 3)
    [line] = _suggest(db, job)["labor_lines"]
    void_invoice(invoice_id=UUID(first), _=USER, db=db)
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [_as_line(line)])
    assert exc.value.status_code == 409
    assert "stale" in json.dumps(exc.value.detail)
    lines = _suggest(db, job)["labor_lines"]
    assert sum(ln["quantity"] for ln in lines) == 3.0, "a fresh read offers the whole restated total"
    _create(db, job, [_as_line(ln) for ln in lines])
    assert _labor_hours(db, job) == Decimal("3")


def test_day_rows_on_a_stale_restatement_line_do_not_slip_past_the_stale_check(db):
    """The restated line also bills a 2 h day row, so its man-hours exceed
    the closeout's delta; voided first invoice between read and save must
    still be refused, and a fresh read bills 3 h + 2 h."""
    job = _job(db)
    first = _billed_first(db, job, 2)
    day = _dt.datetime.now(_dt.UTC).replace(microsecond=0) - _dt.timedelta(days=2)
    db.add(TimeEntry(id=uuid4(), job_id=job.id, tech_id=str(uuid4()), company_id=TENANT,
                     entry_type="job", user_id=ACTOR, clock_in=day,
                     clock_out=day + _dt.timedelta(minutes=120), duration_minutes=120,
                     day_closed_at=day + _dt.timedelta(hours=9)))
    db.commit()
    _close(db, job, 3)
    lines = _suggest(db, job)["labor_lines"]
    assert sum(ln["quantity"] for ln in lines) == 3.0, lines  # 1 h delta + 2 h day
    void_invoice(invoice_id=UUID(first), _=USER, db=db)
    with pytest.raises(HTTPException) as exc:
        _create(db, job, [_as_line(ln) for ln in lines])
    assert exc.value.status_code == 409
    db.rollback()
    fresh = _suggest(db, job)["labor_lines"]
    _create(db, job, [_as_line(ln) for ln in fresh])
    assert _labor_hours(db, job) == Decimal("5"), "3 attested closeout hours + the 2 h day"
    assert _suggest(db, job)["closeout"]["man_hours_left_unbilled"] == 0.0


def _chain(db, job, *, a_billed: bool):
    """A (2 h) -> B (2.5 h) -> C (3 h, live). A and B predate 124 (NULL)."""
    t0 = _dt.datetime(2026, 10, 1, 17, 0, tzinfo=_dt.UTC)
    inv = Invoice(id=uuid4(), job_id=job.id, customer_id=job.customer_id, invoice_number="INV-900002",
                  public_token=uuid4().hex, status="sent", company_id=TENANT)
    db.add(inv)
    db.flush()
    a = JobCloseout(id=uuid4(), job_id=job.id, hours_worked=2, techs_on_site=1, closed_by_user_id=ACTOR,
                    closed_at=t0, superseded_at=t0 + _dt.timedelta(days=1),
                    billed_invoice_id=inv.id if a_billed else None)
    db.add(a)
    db.flush()
    b = JobCloseout(id=uuid4(), job_id=job.id, hours_worked=2.5, techs_on_site=1, closed_by_user_id=ACTOR,
                    closed_at=t0 + _dt.timedelta(days=1), superseded_at=t0 + _dt.timedelta(days=2),
                    supersedes_id=a.id)
    db.add(b)
    db.flush()
    c = JobCloseout(id=uuid4(), job_id=job.id, hours_worked=3, techs_on_site=1, closed_by_user_id=ACTOR,
                    closed_at=t0 + _dt.timedelta(days=2), supersedes_id=b.id, is_restatement=True)
    db.add(c)
    db.commit()
    return t0, c


def test_a_pre_124_re_closeout_in_the_chain_is_read_from_the_audit_trail(db):
    """B was written before 124 and never invoiced; C restates it after the
    deploy. With no uncomplete between A and B, B restated A, so C nets A's
    billed 2 h — a 3 h job bills 3 h, not 5 h."""
    job = _job(db)
    _t0, c = _chain(db, job, a_billed=True)
    assert closeout_hours_billed_before(db, c) == Decimal(2)
    [line] = _suggest(db, job)["labor_lines"]
    assert line["quantity"] == 1.0


def test_a_pre_124_re_closeout_after_an_uncomplete_is_new_work(db):
    job = _job(db)
    t0, c = _chain(db, job, a_billed=True)
    db.add(AuditLog(tenant_id=TENANT, user_id=ACTOR, action="job_uncompleted", entity_type="job",
                    entity_id=str(job.id), details={}, created_at=t0 + _dt.timedelta(hours=12)))
    db.commit()
    assert closeout_hours_billed_before(db, c) == Decimal(0)


def test_a_row_written_before_124_restates_unless_the_job_was_uncompleted(db):
    """``is_restatement`` NULL — every closeout before migration 124 — reads
    the audit trail: no ``job_uncompleted`` between the two closeouts means
    the job stayed completed, so the billed prior is netted."""
    job = _job(db)
    inv = Invoice(id=uuid4(), job_id=job.id, customer_id=job.customer_id, invoice_number="INV-900001",
                  public_token=uuid4().hex, status="sent", company_id=TENANT)
    db.add(inv)
    db.flush()
    now = _dt.datetime.now(_dt.UTC)
    prior = JobCloseout(id=uuid4(), job_id=job.id, hours_worked=2, techs_on_site=1,
                        closed_by_user_id=ACTOR, closed_at=now - _dt.timedelta(hours=2), superseded_at=now,
                        billed_invoice_id=inv.id)
    db.add(prior)
    db.flush()
    current = JobCloseout(id=uuid4(), job_id=job.id, hours_worked=3, techs_on_site=1,
                          closed_by_user_id=ACTOR, closed_at=now, supersedes_id=prior.id)
    db.add(current)
    db.commit()
    assert closeout_hours_billed_before(db, current) == Decimal(2)
    # An explicit False — what closeout_job writes after an uncomplete — never nets.
    current.is_restatement = False
    db.commit()
    assert closeout_hours_billed_before(db, current) == Decimal(0)
