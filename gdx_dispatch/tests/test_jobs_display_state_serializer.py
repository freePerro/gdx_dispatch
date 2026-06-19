"""Slice 4 Wave 0a — the job serializer emits an authoritative
``display_state`` from linked Invoice + originating Estimate rows.

Pins the batched enrichment (`_display_state_for_jobs`) against real ORM
rows in an in-memory DB (same self-contained pattern as
test_jobs_appointment_sync.py). The pure logic is already covered by
test_job_display_state.py; this proves the *wiring* — that real
invoices/estimates get assembled into the derivation correctly, no N+1,
deleted invoices excluded, and the 249-job lie is fixed end-to-end.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401  (register tenant tables)
import gdx_dispatch.modules.proposals.models  # noqa: F401  (register Estimate)
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, Job
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.jobs import _display_state_for_jobs


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


_TENANT = "tenant-test"


def _job(lifecycle_stage: str) -> Job:
    return Job(
        id=uuid.uuid4(),
        title="Garage door install",
        company_id=_TENANT,
        lifecycle_stage=lifecycle_stage,
        dispatch_status="unassigned",
        billing_status="unbilled",
        status=None,
        priority="Normal",
        job_type="Service",
        is_demo=False,
        is_return_visit=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _invoice(job_id, status, balance_due, amount_paid, deleted=False) -> Invoice:
    n = uuid.uuid4().hex[:12]
    return Invoice(
        id=uuid.uuid4(),
        job_id=job_id,
        customer_id=uuid.uuid4(),  # FK not enforced in sqlite test
        company_id=_TENANT,
        invoice_number=f"INV-{n}",
        public_token=f"tok-{n}",
        status=status,
        balance_due=balance_due,
        amount_paid=amount_paid,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
        created_at=datetime.now(timezone.utc),
    )


def _estimate(job_id, status, deleted=False) -> Estimate:
    n = uuid.uuid4().hex[:12]
    return Estimate(
        id=uuid.uuid4(),
        job_id=job_id,
        company_id=_TENANT,
        estimate_number=f"EST-{n}",
        public_token=f"etok-{n}",
        status=status,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
        created_at=datetime.now(timezone.utc),
    )


def _ds(db, job: Job):
    db.commit()
    return _display_state_for_jobs(db, [(job.id, job.lifecycle_stage)]).get(str(job.id))


# --- The 249-job lie, fixed end-to-end -----------------------------------

def test_completed_with_paid_invoice_serializes_as_Paid(db):
    job = _job("completed")
    db.add(job)
    db.add(_invoice(job.id, "paid", 0, 500))
    st = _ds(db, job)
    assert st == {
        "stage": "paid", "type": "won", "label": "Paid", "is_finished": True,
    }


# --- Open work-axis (real prod rows) -------------------------------------

def test_service_call_no_invoice_is_Service_Call(db):
    job = _job("service_call")
    db.add(job)
    st = _ds(db, job)
    assert st["stage"] == "service_call" and st["label"] == "Service Call"
    assert st["type"] == "open" and st["is_finished"] is False


def test_completed_no_invoice_is_Ready_to_Bill(db):
    job = _job("completed")
    db.add(job)
    assert _ds(db, job)["stage"] == "ready_to_bill"


def test_completed_sent_unpaid_is_Invoiced(db):
    job = _job("completed")
    db.add(job)
    db.add(_invoice(job.id, "sent", 250, 0))
    st = _ds(db, job)
    assert st["stage"] == "invoiced" and st["type"] == "open"


# --- Estimate join wiring ------------------------------------------------

def test_declined_estimate_serializes_as_Declined(db):
    job = _job("estimate")
    db.add(job)
    db.add(_estimate(job.id, "declined"))
    st = _ds(db, job)
    assert st == {
        "stage": "declined", "type": "lost", "label": "Declined",
        "is_finished": True,
    }


def test_accepted_estimate_does_not_force_declined(db):
    job = _job("in_progress")
    db.add(job)
    db.add(_estimate(job.id, "accepted"))
    st = _ds(db, job)
    assert st["stage"] == "in_progress" and st["type"] == "open"


# --- Wiring correctness: soft-deleted invoices excluded ------------------

def test_soft_deleted_invoice_excluded(db):
    job = _job("completed")
    db.add(job)
    db.add(_invoice(job.id, "paid", 0, 999, deleted=True))
    # Only invoice is deleted -> treated as no invoice -> Ready to Bill,
    # NOT Paid. Pins the deleted_at filter in the enrichment query.
    assert _ds(db, job)["stage"] == "ready_to_bill"


def test_soft_deleted_declined_estimate_does_not_force_Declined(db):
    # Auditor 2026-05-18: a declined estimate later soft-deleted (replaced/
    # corrected quote — normal workflow) must NOT force the live job to the
    # Declined terminal. Pins Estimate.deleted_at filter symmetry with the
    # Invoice query (the asymmetry that passed every other test).
    job = _job("estimate")
    db.add(job)
    db.add(_estimate(job.id, "declined", deleted=True))
    st = _ds(db, job)
    assert st["stage"] == "estimate" and st["type"] == "open"
    assert st["is_finished"] is False


def test_enrichment_degrades_to_empty_map_on_db_error(db, monkeypatch):
    # The except SQLAlchemyError -> {} branch is the most-defended code;
    # pin that it actually degrades (jobs list must never break over a
    # display field) instead of being untested theater.
    from sqlalchemy.exc import SQLAlchemyError

    # Synthetic inputs — the helper only queries Invoice/Estimate, not the
    # job row, so no persisted job is needed (and avoids an expired-ORM
    # lazy-load tripping the patched execute before the helper runs).
    def _boom(*a, **k):
        raise SQLAlchemyError("simulated DB failure")

    monkeypatch.setattr(db, "execute", _boom)
    assert _display_state_for_jobs(db, [(uuid.uuid4(), "completed")]) == {}


def test_empty_input_returns_empty_map(db):
    assert _display_state_for_jobs(db, []) == {}


def test_batched_multi_job_single_call(db):
    j1, j2, j3 = _job("completed"), _job("service_call"), _job("scheduled")
    db.add_all([j1, j2, j3])
    db.add(_invoice(j1.id, "paid", 0, 100))
    db.commit()
    out = _display_state_for_jobs(
        db,
        [(j1.id, j1.lifecycle_stage), (j2.id, j2.lifecycle_stage), (j3.id, j3.lifecycle_stage)],
    )
    assert out[str(j1.id)]["stage"] == "paid"
    assert out[str(j2.id)]["stage"] == "service_call"
    assert out[str(j3.id)]["stage"] == "scheduled"
