"""Mobile invoicing ownership — the A1 fix (plan §10, audit 2026-07-29).

`mobile_invoicing._job_belongs_to_tech` carried its own ownership SQL whose
first check compared `jobs.assigned_to` to a USER id. That column holds a
TECHNICIAN id in 22 of 22 prod rows, so the check never matched and ownership
fell entirely to the `job_assignments` fallback — present on only 19 of 190
completed jobs. Field billing 404'd for the tech on ~90% of jobs, and
`mobile_invoice_created` has zero audit rows ever: the feature never once ran
in prod. The helper now delegates to `core/job_access.job_belongs_to_user` —
the audited shared gate.

Pinned here:
1. THE PROD SHAPE PASSES: job.assigned_to = technician.id and
   technician.user_id = the calling user → the financial summary answers.
   (SQLite note: `j.id = :j` in the gate compares Uuid text, which is 32-hex
   on SQLite vs dashed on Postgres — so the job id is passed in hex form
   here. The josb-id format is dialect cosmetics; the thing under test is the
   technician→user mapping that the old gate lacked entirely.)
2. A stranger still 404s.
3. Static: mobile_invoicing contains NO bespoke ownership SQL anymore and
   delegates to the shared gate — two implementations that disagree is how
   A1 happened.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
    Technician,
    TimeEntry,
)
from gdx_dispatch.routers.mobile_invoicing import job_financial_summary

TENANT = "tenant-ownership"


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
        JobPartNeeded.__table__,
        Technician.__table__,
        TimeEntry.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _summary(db, job_id: str, user_id: str):
    return job_financial_summary(
        job_id=job_id,
        request=_request(),
        current_user={"user_id": user_id, "sub": user_id, "tenant_id": TENANT},
        db=db,
    )


def test_technician_mapped_ownership_passes(db) -> None:
    """The exact prod shape the old gate could never match: assigned_to holds
    the TECHNICIAN id; the technician row maps to the calling user."""
    user_id = str(uuid4())
    tech = Technician(id=str(uuid4()), company_id=TENANT, user_id=user_id, name="Tech", active=True)
    db.add(tech)
    job = Job(
        customer_id=uuid4(),
        title="ownership job",
        lifecycle_stage="completed",
        dispatch_status="done",
        billing_status="unbilled",
        company_id=TENANT,
        assigned_to=tech.id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    resp = _summary(db, job.id.hex, user_id)
    assert resp.status_code == 200, resp.body[:200]


def test_stranger_still_404s(db) -> None:
    job = Job(
        customer_id=uuid4(),
        title="not yours",
        lifecycle_stage="completed",
        dispatch_status="done",
        billing_status="unbilled",
        company_id=TENANT,
        assigned_to=str(uuid4()),  # some other technician
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    resp = _summary(db, job.id.hex, str(uuid4()))
    assert resp.status_code == 404


def test_no_bespoke_ownership_sql_remains() -> None:
    """Two ownership implementations that disagree is how A1 happened. The
    module must DELEGATE, not re-implement."""
    src = (
        Path(__file__).resolve().parents[1] / "routers/mobile_invoicing.py"
    ).read_text(encoding="utf-8")
    fn = src[src.index("def _job_belongs_to_tech"):src.index("def _next_invoice_number")]
    assert "job_belongs_to_user" in fn, "the helper no longer delegates to the shared gate"
    assert "SELECT 1 FROM jobs" not in fn, "bespoke ownership SQL is back"
    assert "SELECT 1 FROM job_assignments" not in fn, "bespoke ownership SQL is back"
