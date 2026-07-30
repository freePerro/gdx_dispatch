"""Closeout→billing discrepancy detection — plan §12.

A job billed from a closeout that's later revised surfaces for the office. Gated
company-wide; estimate-billed invoices are excluded (agreed price, not a drift).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401 — register all tenant tables
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, JobCloseout

T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _seed_job(db):
    cust = Customer(id=uuid.uuid4(), name="Acme", company_id="t")
    job = Job(id=uuid.uuid4(), title="Door repair", customer_id=cust.id, company_id="t")
    db.add_all([cust, job])
    return job


def _closeout(db, job, *, created, supersedes=None, superseded_at=None, hours=2, techs=1):
    jc = JobCloseout(
        id=uuid.uuid4(), job_id=job.id, hours_worked=hours, techs_on_site=techs,
        closed_by_user_id="u1", closed_at=created, created_at=created,
        supersedes_id=(supersedes.id if supersedes else None), superseded_at=superseded_at,
    )
    db.add(jc)
    return jc


def _invoice(db, job, *, created, estimate_id=None, status="sent"):
    inv = Invoice(
        id=uuid.uuid4(), job_id=job.id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        public_token=uuid.uuid4().hex, customer_id=job.customer_id, company_id="t",
        status=status, total=500, estimate_id=estimate_id, created_at=created,
    )
    db.add(inv)
    return inv


def _seed_revised_and_billed(db):
    """Original closeout → invoice → revised closeout. The classic §12 case."""
    job = _seed_job(db)
    orig = _closeout(db, job, created=T0, superseded_at=T0 + timedelta(days=2), hours=2)
    _invoice(db, job, created=T0 + timedelta(days=1))          # billed against orig
    _closeout(db, job, created=T0 + timedelta(days=2), supersedes=orig, hours=5)  # revised, now live
    db.commit()
    return job


def test_detects_reclose_after_billing_when_enabled(db, monkeypatch):
    import gdx_dispatch.core.closeout_reconciliation as m
    monkeypatch.setattr(m, "closeout_reconciliation_enabled", lambda _tid: True)
    job = _seed_revised_and_billed(db)

    result = m.find_closeout_billing_discrepancies(db, "t")
    assert result["enabled"] is True
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["job_id"] == str(job.id)
    assert item["billed_against"]["hours_worked"] == 2
    assert item["current_closeout"]["hours_worked"] == 5
    assert item["invoice"]["invoice_number"].startswith("INV-")


def test_disabled_toggle_returns_nothing(db, monkeypatch):
    import gdx_dispatch.core.closeout_reconciliation as m
    monkeypatch.setattr(m, "closeout_reconciliation_enabled", lambda _tid: False)
    _seed_revised_and_billed(db)

    result = m.find_closeout_billing_discrepancies(db, "t")
    assert result == {"enabled": False, "items": []}


def test_estimate_billed_invoice_is_not_a_discrepancy(db, monkeypatch):
    import gdx_dispatch.core.closeout_reconciliation as m
    monkeypatch.setattr(m, "closeout_reconciliation_enabled", lambda _tid: True)
    job = _seed_job(db)
    orig = _closeout(db, job, created=T0, superseded_at=T0 + timedelta(days=2))
    _invoice(db, job, created=T0 + timedelta(days=1), estimate_id=uuid.uuid4())  # agreed price
    _closeout(db, job, created=T0 + timedelta(days=2), supersedes=orig, hours=5)
    db.commit()

    assert m.find_closeout_billing_discrepancies(db, "t")["items"] == []


def test_detects_historical_revision_with_null_supersedes_id(db, monkeypatch):
    """Migration 041 backfilled pre-existing duplicate closeouts with
    superseded_at set but supersedes_id NULL ('the chain is unknowable'). The
    detector keys on superseded_at, so it must still flag these."""
    import gdx_dispatch.core.closeout_reconciliation as m
    monkeypatch.setattr(m, "closeout_reconciliation_enabled", lambda _tid: True)
    job = _seed_job(db)
    # 041-style: an older row stamped superseded_at, NO supersedes_id link.
    _closeout(db, job, created=T0, superseded_at=T0 + timedelta(days=2), hours=2)
    _invoice(db, job, created=T0 + timedelta(days=1))
    _closeout(db, job, created=T0 + timedelta(days=2), supersedes=None, hours=5)  # live, supersedes_id NULL
    db.commit()

    result = m.find_closeout_billing_discrepancies(db, "t")
    assert len(result["items"]) == 1
    assert result["items"][0]["current_closeout"]["hours_worked"] == 5
    assert result["items"][0]["billed_against"]["hours_worked"] == 2


def test_first_time_closeout_is_not_a_discrepancy(db, monkeypatch):
    """A never-revised closeout (supersedes_id NULL) is normal, not a drift."""
    import gdx_dispatch.core.closeout_reconciliation as m
    monkeypatch.setattr(m, "closeout_reconciliation_enabled", lambda _tid: True)
    job = _seed_job(db)
    _closeout(db, job, created=T0)                       # first + only closeout
    _invoice(db, job, created=T0 + timedelta(days=1))
    db.commit()

    assert m.find_closeout_billing_discrepancies(db, "t")["items"] == []
