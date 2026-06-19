"""S97 slice 8 — labor variance per job.

Direct handler tests for ``GET /api/jobs/{job_id}/labor-variance`` covering
the rate-resolution hierarchy (PayrollEntry → Technician → User → none) and
the wall-clock math (per-tech arrived_at → completed_at, summed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from gdx_dispatch.models.tenant_models import Job, JobAssignment, PayrollEntry, Technician, User
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.labor_variance import labor_variance


def _stub_user():
    return {"sub": "u-1", "user_id": "u-1"}


def _stub_request():
    req = MagicMock()
    req.state.tenant = {"id": "t-test"}
    req.headers = {}
    req.client = MagicMock(host="127.0.0.1")
    return req


def _mk_job(db, *, completed_at=None):
    j = Job(
        id=uuid4(),
        job_number=f"J-{uuid4().hex[:6]}",
        title="install",
        status="Complete" if completed_at else "Scheduled",
        scheduled_at=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc),
        completed_at=completed_at,
        company_id="t-test",
    )
    db.add(j)
    db.commit()
    return j


def _mk_estimate_with_hours(db, job, hours_list):
    est = Estimate(
        id=uuid4(),
        job_id=job.id,
        estimate_number=f"E-{uuid4().hex[:6]}",
        public_token=f"tok-{uuid4().hex[:8]}",
        company_id="t-test",
        status="accepted",
        accepted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        total=Decimal("0.00"),
    )
    db.add(est)
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
            estimated_man_hours=Decimal(str(hrs)),
        ))
    db.commit()
    return est


def _assign(db, job, *, tech_id=None, user_id=None, is_lead=False, hours_worked=None):
    """Adds a JobAssignment. If hours_worked given, sets arrived_at/completed_at to span that."""
    arrived = completed = None
    if hours_worked is not None:
        arrived = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)
        completed = arrived + timedelta(hours=float(hours_worked))
    a = JobAssignment(
        id=str(uuid4()),
        job_id=str(job.id),
        tech_id=tech_id or str(uuid4()),
        user_id=user_id,
        is_lead=is_lead,
        assigned_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        arrived_at=arrived,
        completed_at=completed,
    )
    db.add(a)
    db.commit()
    return a


def _mk_technician(db, *, user_id, hourly_rate=None):
    t = Technician(
        id=str(uuid4()),
        company_id="t-test",
        user_id=user_id,
        hourly_rate=Decimal(str(hourly_rate)) if hourly_rate is not None else None,
        active=True,
    )
    db.add(t)
    db.commit()
    return t


def _mk_user(db, *, hourly_rate=None):
    u = User(
        id=uuid4(),
        email=f"u-{uuid4().hex[:6]}@t.test",
        password_hash="x",
        role="tech",
        company_id="t-test",
        hourly_rate=Decimal(str(hourly_rate)) if hourly_rate is not None else None,
    )
    db.add(u)
    db.commit()
    return u


def _mk_payroll(db, *, tech_user_id, gross_pay, hours_paid, period=(datetime(2026, 4, 27, tzinfo=timezone.utc), datetime(2026, 5, 3, tzinfo=timezone.utc))):
    p = PayrollEntry(
        id=uuid4(),
        company_id="t-test",
        tech_user_id=tech_user_id,
        period_start=period[0],
        period_end=period[1],
        hours_paid=Decimal(str(hours_paid)),
        gross_pay=Decimal(str(gross_pay)),
        source="manual",
    )
    db.add(p)
    db.commit()
    return p


def test_unknown_job_returns_404(tenant_db):
    with pytest.raises(HTTPException) as exc:
        labor_variance(
            job_id=uuid4(),
            request=_stub_request(),
            user=_stub_user(),
            db=tenant_db,
        )
    assert exc.value.status_code == 404


def test_no_estimate_zero_estimated(tenant_db):
    job = _mk_job(tenant_db)
    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["estimated_hours"] == 0.0
    assert out["estimate_id"] is None


def test_no_assignments_zero_actual(tenant_db):
    job = _mk_job(tenant_db)
    _mk_estimate_with_hours(tenant_db, job, [5.0])
    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["estimated_hours"] == 5.0
    assert out["actual_hours"] == 0.0
    assert out["per_tech"] == []


def test_payroll_rate_wins(tenant_db):
    """Rate hierarchy: PayrollEntry beats Technician.hourly_rate."""
    job = _mk_job(tenant_db, completed_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc))
    _mk_estimate_with_hours(tenant_db, job, [5.0])
    user = _mk_user(tenant_db, hourly_rate=40.0)
    tech = _mk_technician(tenant_db, user_id=str(user.id), hourly_rate=50.0)
    # PayrollEntry says actual paid rate is $80/hr (gross 800 / hours 10).
    _mk_payroll(tenant_db, tech_user_id=str(user.id), gross_pay=800.0, hours_paid=10.0)
    _assign(tenant_db, job, tech_id=tech.id, user_id=str(user.id), is_lead=True, hours_worked=4.0)

    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["actual_hours"] == 4.0
    assert out["estimated_hours"] == 5.0
    assert out["primary_rate"] == 80.0
    assert out["primary_rate_source"] == "payroll"
    assert out["actual_cost"] == 320.0  # 4h × $80
    assert out["estimated_cost"] == 400.0  # 5h × $80
    assert out["variance_hours"] == -1.0
    assert out["variance_cost"] == -80.0


def test_technician_rate_when_no_payroll(tenant_db):
    """Falls back to Technician.hourly_rate when no payroll covers the date."""
    job = _mk_job(tenant_db, completed_at=datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc))
    _mk_estimate_with_hours(tenant_db, job, [3.0])
    user = _mk_user(tenant_db, hourly_rate=40.0)
    tech = _mk_technician(tenant_db, user_id=str(user.id), hourly_rate=60.0)
    _assign(tenant_db, job, tech_id=tech.id, user_id=str(user.id), is_lead=True, hours_worked=3.0)

    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["primary_rate"] == 60.0
    assert out["primary_rate_source"] == "technician"
    assert out["actual_cost"] == 180.0
    assert out["estimated_cost"] == 180.0
    assert out["variance_cost"] == 0.0


def test_user_rate_last_resort(tenant_db):
    """Falls back to User.hourly_rate when Technician has no rate."""
    job = _mk_job(tenant_db, completed_at=datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc))
    _mk_estimate_with_hours(tenant_db, job, [2.0])
    user = _mk_user(tenant_db, hourly_rate=35.0)
    tech = _mk_technician(tenant_db, user_id=str(user.id), hourly_rate=None)
    _assign(tenant_db, job, tech_id=tech.id, user_id=str(user.id), is_lead=True, hours_worked=2.0)

    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["primary_rate"] == 35.0
    assert out["primary_rate_source"] == "user"


def test_two_techs_summed(tenant_db):
    """Two assigned techs each work 2h → 4h actual, both at the same rate."""
    job = _mk_job(tenant_db, completed_at=datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc))
    _mk_estimate_with_hours(tenant_db, job, [5.0])
    u1 = _mk_user(tenant_db)
    u2 = _mk_user(tenant_db)
    t1 = _mk_technician(tenant_db, user_id=str(u1.id), hourly_rate=50.0)
    t2 = _mk_technician(tenant_db, user_id=str(u2.id), hourly_rate=50.0)
    _assign(tenant_db, job, tech_id=t1.id, user_id=str(u1.id), is_lead=True, hours_worked=2.0)
    _assign(tenant_db, job, tech_id=t2.id, user_id=str(u2.id), hours_worked=2.0)

    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["actual_hours"] == 4.0
    assert out["actual_cost"] == 200.0  # 4h × $50
    assert len(out["per_tech"]) == 2
    assert all(t["rate_source"] == "technician" for t in out["per_tech"])


def test_no_rate_anywhere_marks_none(tenant_db):
    """When no rate can be resolved, cost is 0 and source is 'none'."""
    job = _mk_job(tenant_db, completed_at=datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc))
    _mk_estimate_with_hours(tenant_db, job, [1.0])
    _assign(tenant_db, job, hours_worked=1.0, is_lead=True)

    out = labor_variance(
        job_id=job.id,
        request=_stub_request(),
        user=_stub_user(),
        db=tenant_db,
    )
    assert out["primary_rate"] is None
    assert out["primary_rate_source"] == "none"
    assert out["actual_cost"] == 0.0
    assert out["estimated_cost"] == 0.0
