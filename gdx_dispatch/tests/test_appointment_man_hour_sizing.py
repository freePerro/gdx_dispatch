"""S97 slice 7 — appointment block sizing from labor matrix man-hours.

Direct unit tests on ``compute_man_hour_duration_minutes`` so they exercise
the math (sum_man_hours / crew_size, rounded UP to 15 min) without spinning
up the FastAPI handler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.models.tenant_models import Job, JobAssignment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.appointments import compute_man_hour_duration_minutes


def _job(db, *, num="J-S97"):
    j = Job(
        id=uuid4(),
        job_number=num,
        title="install",
        status="Scheduled",
        company_id="t-test",
    )
    db.add(j)
    db.commit()
    return j


def _accepted_estimate_with_hours(db, job, hours_list, *, status="accepted"):
    est = Estimate(
        id=uuid4(),
        job_id=job.id,
        estimate_number=f"EST-{uuid4().hex[:6]}",
        public_token=f"tok-{uuid4().hex[:8]}",
        company_id="t-test",
        status=status,
        accepted_at=datetime.now(timezone.utc) if status == "accepted" else None,
        total=Decimal("0.00"),
    )
    db.add(est)
    db.flush()
    row = LaborPriceItem(
        id=uuid4(),
        description="install",
        service_type="install",
        flat_price=Decimal("500.00"),
        assumed_man_hours=Decimal("5.00"),
    )
    db.add(row)
    db.flush()
    for hrs in hours_list:
        db.add(EstimateLine(
            id=uuid4(),
            estimate_id=est.id,
            description="labor",
            quantity=1,
            unit_price=Decimal("500.00"),
            line_total=Decimal("500.00"),
            sort_order=1,
            company_id="t-test",
            labor_price_item_id=row.id,
            estimated_man_hours=Decimal(str(hrs)),
        ))
    db.commit()
    return est


def _assign(db, job, n):
    for _ in range(n):
        db.add(JobAssignment(
            id=str(uuid4()),
            job_id=str(job.id),
            tech_id=str(uuid4()),
            assigned_at=datetime.now(timezone.utc),
        ))
    db.commit()


def test_no_estimate_returns_none(tenant_db):
    job = _job(tenant_db)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) is None


def test_estimate_without_man_hours_returns_none(tenant_db):
    job = _job(tenant_db)
    # Empty hours list → no labor lines with hours.
    _accepted_estimate_with_hours(tenant_db, job, [])
    assert compute_man_hour_duration_minutes(tenant_db, job.id) is None


def test_one_tech_5h_returns_300_min(tenant_db):
    """5 man-hours / crew=1 = 5h = 300 min (already a 15-min multiple)."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [5.0])
    _assign(tenant_db, job, 1)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 300


def test_two_techs_halve_wallclock(tenant_db):
    """5 man-hours / crew=2 = 2.5h = 150 min."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [5.0])
    _assign(tenant_db, job, 2)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 150


def test_no_assignments_treated_as_crew_one(tenant_db):
    """5 man-hours, no JobAssignments → defaults to crew=1 = 300 min."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [5.0])
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 300


def test_rounds_up_to_15_min(tenant_db):
    """1.1 man-hours / crew=1 = 66 min → rounds up to 75 min."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [1.1])
    _assign(tenant_db, job, 1)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 75


def test_sums_lines(tenant_db):
    """3 + 2 + 1 = 6 man-hours / crew=2 = 180 min."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [3.0, 2.0, 1.0])
    _assign(tenant_db, job, 2)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 180


def test_falls_back_to_draft_estimate(tenant_db):
    """No accepted estimate, but a draft has hours → use it for the suggestion."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [4.0], status="draft")
    _assign(tenant_db, job, 2)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 120


def test_minimum_15_min(tenant_db):
    """Tiny job (5 minutes worth) still rounds up to 15 min minimum."""
    job = _job(tenant_db)
    _accepted_estimate_with_hours(tenant_db, job, [0.05])  # 3 min
    _assign(tenant_db, job, 1)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 15
