"""A lead follows its selected estimate through to Paid — lead-to-paid PR B.

GET /api/leads carries `progress`, derived on every read from the lead's
estimates, the selected estimate's job and that job's invoices. Real routers
over a real (SQLite) schema; the same harness as test_lead_estimate_link.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, Job
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.tests.test_lead_estimate_link import (
    TENANT,
    _client,
    _db,
    _lead,
    _mark_sent,
    _started,
)


@pytest.fixture()
def sales():
    tc = _client("sales")
    yield tc
    tc._engine.dispose()  # type: ignore[attr-defined]


def _progress(tc, lead_id):
    rows = tc.get("/api/leads").json()
    return next(r for r in rows if r["id"] == lead_id)["progress"]


def _accepted(tc, lead_id):
    est = _started(tc, lead_id)["estimate"]["id"]
    _mark_sent(tc, est)
    r = tc.post(f"/api/estimates/{est}/accept", json={})
    assert r.status_code == 200, r.text
    return est, r.json().get("auto_converted_job_id") or _job_of(tc, est)


def _job_of(tc, est_id):
    db = _db(tc)
    try:
        return str(db.execute(select(Estimate.job_id).where(Estimate.id == uuid.UUID(est_id))).scalar_one())
    finally:
        db.close()


def _invoice(tc, job_id, *, status, balance_due):
    db = _db(tc)
    n = uuid.uuid4().hex[:6]
    db.add(Invoice(
        id=uuid.uuid4(), job_id=uuid.UUID(job_id), customer_id=uuid.uuid4(),
        company_id=TENANT, invoice_number=f"INV-{n}", public_token=f"tok-{n}",
        status=status, balance_due=Decimal(balance_due), billing_type="standard",
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.close()


def test_a_lead_with_no_estimates_has_no_progress(sales):
    lead_id = _lead(sales)
    assert _progress(sales, lead_id) is None


def test_a_started_draft_reads_estimate_started(sales):
    lead_id = _lead(sales)
    _started(sales, lead_id)
    p = _progress(sales, lead_id)
    assert p["label"] == "Estimate started" and p["type"] == "open"


def test_a_sent_estimate_reads_quoted(sales):
    lead_id = _lead(sales)
    _mark_sent(sales, _started(sales, lead_id)["estimate"]["id"])
    assert _progress(sales, lead_id)["label"] == "Quoted"


def test_accepted_follows_the_job_and_carries_its_schedule(sales):
    lead_id = _lead(sales)
    _est, job_id = _accepted(sales, lead_id)
    p = _progress(sales, lead_id)
    assert p["stage"] == "scheduled"  # accept lands the job in scheduled
    assert "scheduled_at" in p  # so the chip can say "Awaiting Schedule"


def test_paid_invoice_reads_paid_and_won(sales):
    lead_id = _lead(sales)
    _est, job_id = _accepted(sales, lead_id)
    db = _db(sales)
    job = db.execute(select(Job).where(Job.id == uuid.UUID(job_id))).scalar_one()
    job.lifecycle_stage = "completed"
    db.commit()
    db.close()
    _invoice(sales, job_id, status="paid", balance_due="0")
    p = _progress(sales, lead_id)
    assert p["label"] == "Paid" and p["type"] == "won"


def test_a_sent_invoice_reads_invoiced(sales):
    lead_id = _lead(sales)
    _est, job_id = _accepted(sales, lead_id)
    db = _db(sales)
    job = db.execute(select(Job).where(Job.id == uuid.UUID(job_id))).scalar_one()
    job.lifecycle_stage = "completed"
    db.commit()
    db.close()
    _invoice(sales, job_id, status="sent", balance_due="500")
    assert _progress(sales, lead_id)["stage"] == "invoiced"


def test_sold_before_there_is_a_job(sales):
    lead_id = _lead(sales)
    _est, job_id = _accepted(sales, lead_id)
    db = _db(sales)
    job = db.execute(select(Job).where(Job.id == uuid.UUID(job_id))).scalar_one()
    job.deleted_at = datetime.now(timezone.utc)
    db.commit()
    db.close()
    assert _progress(sales, lead_id)["label"] == "Sold"


def test_all_declined_reads_declined_and_lost(sales):
    lead_id = _lead(sales)
    est = _started(sales, lead_id)["estimate"]["id"]
    _mark_sent(sales, est)
    r = sales.post(f"/api/estimates/{est}/decline", json={"reason": "went elsewhere"})
    assert r.status_code == 200, r.text
    p = _progress(sales, lead_id)
    assert p["label"] == "Declined" and p["type"] == "lost"


def test_the_selected_estimate_drives_it_not_the_newest(sales):
    """Two accepted options: progress follows the pick, and moves with it."""
    lead_id = _lead(sales)
    first, first_job = _accepted(sales, lead_id)
    second = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    _mark_sent(sales, second)
    sales.post(f"/api/estimates/{second}/accept", json={})
    second_job = _job_of(sales, second)
    db = _db(sales)
    job = db.execute(select(Job).where(Job.id == uuid.UUID(first_job))).scalar_one()
    job.lifecycle_stage = "completed"
    db.commit()
    db.close()
    _invoice(sales, first_job, status="paid", balance_due="0")
    assert _progress(sales, lead_id)["label"] == "Paid"  # first is the pick
    assert sales.put(f"/api/leads/{lead_id}/selected-estimate",
                     json={"estimate_id": second}).status_code == 200
    assert _progress(sales, lead_id)["stage"] == "scheduled"
    assert second_job != first_job


def test_a_won_lead_with_its_pick_cleared_never_reads_quoted(sales):
    """Clearing the pick while another option is still out must not demote a
    sold (even paid) lead to "Quoted" — accepted outranks sent and draft."""
    lead_id = _lead(sales)
    first, _job = _accepted(sales, lead_id)
    other = sales.post(f"/api/estimates/{first}/duplicate", json={}).json()["id"]
    _mark_sent(sales, other)
    assert sales.put(f"/api/leads/{lead_id}/selected-estimate",
                     json={"estimate_id": None}).status_code == 200
    assert _progress(sales, lead_id)["label"] == "Sold"


def test_an_underivable_job_state_reads_unknown_not_sold(sales, monkeypatch):
    """The jobs helper degrades to {} on a DB error (it never raises). A paid
    lead must then show no progress, not a false "Sold"."""
    import gdx_dispatch.routers.jobs as jobs_router

    lead_id = _lead(sales)
    _est, job_id = _accepted(sales, lead_id)
    db = _db(sales)
    job = db.execute(select(Job).where(Job.id == uuid.UUID(job_id))).scalar_one()
    job.lifecycle_stage = "completed"
    db.commit()
    db.close()
    _invoice(sales, job_id, status="paid", balance_due="0")
    assert _progress(sales, lead_id)["label"] == "Paid"
    monkeypatch.setattr(jobs_router, "_display_state_for_jobs", lambda *a, **k: {})
    r = sales.get("/api/leads")
    assert r.status_code == 200
    assert next(x for x in r.json() if x["id"] == lead_id)["progress"] is None
