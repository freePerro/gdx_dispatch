"""PR2-billing-capture (2026-07-07) — the canonical "billed" predicate.

`Job.billing_status` is a dead cache (only ever written "unbilled"), so every
reader that filtered on it was wrong. These tests pin:

1. The predicate matrix — void-only and $0-DRAFT-only invoices do NOT bill a
   job; a $0 SENT invoice (deliberate freebie) and any >$0 live invoice DO.
2. The Python twin (`invoice_bills_job`) stays in lockstep with the SQL
   EXISTS clause over the same matrix.
3. Parity — /api/jobs/ready-for-billing and /api/invoices/summary's
   ready_for_billing count agree on the same fixture set.
4. invoice_now derives from invoices: no more nagging on paid jobs; a job
   whose only invoice was VOIDED alerts again.
5. The stale-estimate next-action still fires after the dead billing_status
   clause deletion (zero behavior change).
6. The forecasting scheduled-jobs projection ignores billing_status entirely
   (the deleted clause was a tautology; binary include semantics unchanged —
   deposit-subtraction is a separate, /audit-gated follow-up).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.billing_predicates import invoice_bills_job, job_billed_exists
from gdx_dispatch.core.next_action import NextActionQueue
from gdx_dispatch.core.recommendations import RecommendationEngine
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job, Payment
from gdx_dispatch.modules.forecasting.models import ForecastSettings
from gdx_dispatch.modules.forecasting.service import _scheduled_jobs_projection
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import billing_summary
from gdx_dispatch.routers.jobs import ready_for_billing


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
        Estimate.__table__,
        EstimateLine.__table__,
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


def _seed_job(db, *, stage: str = "completed", title: str = "Job", **kw) -> Job:
    job = Job(
        customer_id=uuid4(),
        title=title,
        description="t",
        lifecycle_stage=stage,
        dispatch_status="done",
        billing_status=kw.pop("billing_status", "unbilled"),
        company_id="tenant-1",
        **kw,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_invoice(db, job, *, status: str, total: float, deleted: bool = False) -> Invoice:
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
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(inv)
    db.commit()
    return inv


# (label, invoice-config-or-None, expected_billed)
MATRIX = [
    ("no_invoice", None, False),
    ("sent_500", {"status": "sent", "total": 500.0}, True),
    ("draft_500", {"status": "draft", "total": 500.0}, True),
    ("void_only", {"status": "void", "total": 500.0}, False),
    ("zero_draft", {"status": "draft", "total": 0.0}, False),
    ("zero_sent", {"status": "sent", "total": 0.0}, True),
    ("deleted_sent", {"status": "sent", "total": 500.0, "deleted": True}, False),
]


def _seed_matrix(db) -> dict[str, Job]:
    jobs = {}
    for label, inv_cfg, _expected in MATRIX:
        job = _seed_job(db, title=label)
        if inv_cfg:
            _seed_invoice(db, job, **inv_cfg)
        jobs[label] = job
    return jobs


def test_job_billed_exists_matrix(tenant_db_session):
    db = tenant_db_session
    jobs = _seed_matrix(db)

    billed_ids = {
        str(row) for row in db.execute(
            select(Job.id).where(job_billed_exists())
        ).scalars().all()
    }
    for label, _cfg, expected in MATRIX:
        actual = str(jobs[label].id) in billed_ids
        assert actual is expected, f"{label}: billed={actual}, expected {expected}"


def test_python_twin_matches_sql(tenant_db_session):
    """invoice_bills_job must agree with the EXISTS clause row-for-row."""
    db = tenant_db_session
    jobs = _seed_matrix(db)
    billed_ids = {
        str(row) for row in db.execute(
            select(Job.id).where(job_billed_exists())
        ).scalars().all()
    }
    for label, _cfg, _expected in MATRIX:
        job = jobs[label]
        invs = db.execute(select(Invoice).where(Invoice.job_id == job.id)).scalars().all()
        py_billed = any(
            invoice_bills_job(i.status, float(i.total or 0), i.deleted_at) for i in invs
        )
        assert py_billed == (str(job.id) in billed_ids), label


def test_rfb_endpoint_and_summary_count_agree(tenant_db_session):
    db = tenant_db_session
    jobs = _seed_matrix(db)
    expected_unbilled = {
        str(jobs[label].id) for label, _cfg, billed in MATRIX if not billed
    }

    rfb_rows = ready_for_billing(request=None, current_user=_current_user(), db=db)
    rfb_ids = {r["id"] for r in rfb_rows}
    assert rfb_ids == expected_unbilled

    summary = billing_summary(request=None, _=_current_user(), db=db)
    assert summary["ready_for_billing"] == len(expected_unbilled)


def test_invoice_now_no_longer_fires_for_billed_jobs(tenant_db_session):
    """The old rule read the dead cache and nagged PAID jobs forever."""
    db = tenant_db_session
    job = _seed_job(db, title="paid job")
    _seed_invoice(db, job, status="paid", total=500.0)

    recs = RecommendationEngine().get_job_recommendations("tenant-1", str(job.id), db)
    assert "invoice_now" not in {r["type"] for r in recs}


def test_invoice_now_fires_for_void_only_job(tenant_db_session):
    """A job whose only invoice was voided is UNBILLED — the old LEFT-JOIN
    semantics hid it forever."""
    db = tenant_db_session
    job = _seed_job(db, title="void job")
    _seed_invoice(db, job, status="void", total=500.0)

    recs = RecommendationEngine().get_job_recommendations("tenant-1", str(job.id), db)
    assert "invoice_now" in {r["type"] for r in recs}


def test_stale_estimate_action_survives_clause_deletion(tenant_db_session):
    """The billing_status filter deleted from the stale-estimate rule was a
    tautology — the rule must fire exactly as before."""
    db = tenant_db_session
    job = _seed_job(db, stage="estimate", title="old estimate")
    job.created_at = datetime.now(UTC) - timedelta(hours=100)
    db.commit()

    actions = NextActionQueue().get_auto_actions("tenant-1", db)
    assert any(a["action_type"] == "follow_up_estimate" for a in actions)


def test_scheduled_projection_ignores_billing_status(tenant_db_session):
    """The deleted forecasting clause was a tautology on real data. Pin the
    new invariant: the projection keys ONLY off stage/schedule/window —
    billing_status (any value) and existing invoices don't exclude a job.
    (Subtracting already-invoiced deposits is a separate, /audit-gated
    follow-up — this documents that today's semantics are binary.)"""
    db = tenant_db_session
    tomorrow = datetime.now(UTC) + timedelta(days=1)

    j1 = _seed_job(db, stage="scheduled", title="normal", scheduled_at=tomorrow)
    j2 = _seed_job(
        db, stage="scheduled", title="weird status",
        scheduled_at=tomorrow, billing_status="paid",
    )
    for j in (j1, j2):
        db.add(Estimate(
            job_id=j.id,
            customer_id=j.customer_id,
            estimate_number=f"EST-{uuid4().hex[:8]}",
            label="e",
            proposal_mode=False,
            total=Decimal("500.00"),
            status="accepted",
            public_token=uuid4().hex,
            company_id="tenant-1",
        ))
    db.commit()
    _seed_invoice(db, j1, status="sent", total=250.0)  # deposit-style invoice

    settings = ForecastSettings(scheduled_realization_rate=Decimal("0.5"))
    out = _scheduled_jobs_projection(db, settings, date.today(), 30)

    included_titles = {j["title"] for j in out["jobs"]}
    assert included_titles == {"normal", "weird status"}
    assert out["scheduled_total"] == 1000.0
    assert out["expected_total"] == 500.0
