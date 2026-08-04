"""Dashboard return-visits queue — GET /api/jobs/return-visits-unscheduled.

PR #267 made the closeout mint an unscheduled, unassigned child job when the
tech attests "I need to come back". Nothing surfaced those children: no board
leads with unscheduled work, so the trip only happened if dispatch remembered
it existed. This endpoint is the dashboard's count of them.

Contract, pinned here:
1. A closeout-minted return visit (unscheduled, open) is listed, carrying the
   attested WHY in ``description`` and the parent link.
2. Scheduling the child removes it from the queue (scheduled_at set).
3. Dealing with it in ANY other way also removes it: assigning a tech
   (spawn-return-visit can pre-assign without a date) or parking it in a
   holding area (Waiting on Parts is the DESIGNED state for a return visit
   whose part is on order — counting parked jobs would nag forever and
   teach everyone to ignore the entry).
4. Completed/cancelled children are excluded (stage predicate matching the
   closeout's open-child check).
5. Soft-deleted children and ordinary (non-return-visit) unscheduled jobs
   never appear.
6. Oldest first — the customer who has waited longest leads.

Harness mirrors test_closeout_return_visit.py: router functions invoked
directly against in-memory SQLite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.routers.jobs import return_visits_unscheduled

TENANT = "tenant-rv-dashboard"
USER = {"user_id": "user-doug", "tenant_id": TENANT, "role": "dispatcher"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [Job.__table__, Customer.__table__]:
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


def _seed_customer(db, name="Ana Winters") -> Customer:
    cust = Customer(id=uuid4(), name=name, company_id=TENANT)
    db.add(cust)
    db.commit()
    return cust


def _seed_return_visit(db, customer, **overrides) -> Job:
    fields = dict(
        id=uuid4(),
        title="Return visit: Broken torsion spring",
        description="Spring on order — needs a second trip to install.",
        customer_id=customer.id if customer else None,
        parent_job_id=uuid4(),
        company_id=TENANT,
        status="Service Call",
        lifecycle_stage="service_call",
        dispatch_status="unassigned",
        billing_status="unbilled",
        is_return_visit=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


def _rows(db) -> list[dict]:
    return return_visits_unscheduled(request=_request(), current_user=USER, db=db)


def test_unscheduled_return_visit_is_listed_with_reason_and_parent(db) -> None:
    cust = _seed_customer(db)
    job = _seed_return_visit(db, cust)

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(job.id)
    assert row["customer_name"] == "Ana Winters"
    assert row["description"] == "Spring on order — needs a second trip to install."
    assert row["parent_job_id"] == str(job.parent_job_id)


def test_scheduling_the_child_clears_it_from_the_queue(db) -> None:
    cust = _seed_customer(db)
    job = _seed_return_visit(db, cust)
    assert len(_rows(db)) == 1

    job.scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    db.commit()
    assert _rows(db) == []


def test_parked_in_holding_area_stops_counting(db) -> None:
    """Waiting on Parts is the designed workflow for a closeout return visit
    whose part is on order — parked means dispatch HAS dealt with it, and the
    holding-area board is its surface. A permanent phantom entry here is how
    the queue gets ignored."""
    cust = _seed_customer(db)
    job = _seed_return_visit(db, cust)
    assert len(_rows(db)) == 1

    job.holding_area_id = "ha-waiting-on-parts"
    db.commit()
    assert _rows(db) == []


def test_assigned_without_a_date_stops_counting(db) -> None:
    """spawn-return-visit can pre-assign a tech without scheduling — the job
    is on that tech's board, not lost."""
    cust = _seed_customer(db)
    _seed_return_visit(db, cust, assigned_to="tech-gus")
    assert _rows(db) == []


def test_completed_cancelled_and_deleted_children_are_excluded(db) -> None:
    cust = _seed_customer(db)
    _seed_return_visit(db, cust, lifecycle_stage="completed")
    _seed_return_visit(db, cust, lifecycle_stage="cancelled")
    _seed_return_visit(db, cust, deleted_at=datetime.now(timezone.utc))
    assert _rows(db) == []


def test_ordinary_unscheduled_jobs_are_not_return_visits(db) -> None:
    cust = _seed_customer(db)
    _seed_return_visit(db, cust, is_return_visit=False, parent_job_id=None)
    assert _rows(db) == []


def test_oldest_waiting_leads(db) -> None:
    cust = _seed_customer(db)
    now = datetime.now(timezone.utc)
    newer = _seed_return_visit(db, cust, created_at=now)
    older = _seed_return_visit(db, cust, created_at=now - timedelta(days=3))

    rows = _rows(db)
    assert [r["id"] for r in rows] == [str(older.id), str(newer.id)]


def test_missing_customer_renders_empty_name_not_crash(db) -> None:
    _seed_return_visit(db, customer=None, customer_id=uuid4())
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["customer_name"] == ""
