"""Multi-day jobs PR 3, the phone's half (plan §5.4a: "The timer of a
forgotten day (B8)" and "The phone on day 2 (B3, B4, B7)").

Every test drives the real route through a TestClient on a fresh SQLite
schema built from the ORM. The routes are called with the job's 32-hex id:
on SQLite the raw-SQL job read and the raw-SQL timer insert only line up with
the ORM-written rows in that form (CLAUDE.md, "SQLite stores a Uuid column as
32 dashless hex"), and the day-close module reads timers through the ORM.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobAssignment,
    Technician,
    TimeEntry,
)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-day-close-mobile"
TECH_USER = str(uuid4())
TECH = str(uuid4())
TECH2_USER = str(uuid4())
TECH2 = str(uuid4())
NY = ZoneInfo("America/New_York")  # the shop zone with no AppSettings row


def _utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setattr(mobile_router, "_assert_job_access", lambda *a, **k: None)
    monkeypatch.setattr(mobile_router, "_assert_job_read_access", lambda *a, **k: "assigned")
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user
    from gdx_dispatch.routers.jobs import router as jobs_router

    caller: dict = {}
    app = FastAPI()
    app.include_router(jobs_router)
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller
    for key in ("jobs", "mobile"):
        app.dependency_overrides[require_module(key)] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "dcm"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    def be(user_id: str) -> None:
        caller.clear()
        caller.update({"user_id": user_id, "sub": user_id, "tenant_id": TENANT,
                       "role": "technician"})

    db.add(Technician(id=TECH, company_id=TENANT, user_id=TECH_USER, active=True))
    db.add(Technician(id=TECH2, company_id=TENANT, user_id=TECH2_USER, active=True))
    db.commit()
    be(TECH_USER)
    yield TestClient(app, raise_server_exceptions=False), db, be
    db.close()
    engine.dispose()


def _job(db, *, dispatch_status="assigned", stage="in_progress", started_at=None,
         crew=(TECH,)) -> Job:
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    job = Job(
        id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Install 16x7",
        description="", scheduled_at=_now(), status="Scheduled", priority="Normal",
        job_type="Install", lifecycle_stage=stage, assigned_to=crew[0],
        dispatch_status=dispatch_status, billing_status="unbilled", is_demo=False,
        is_return_visit=False, started_at=started_at,
    )
    db.add(job)
    db.flush()
    for i, tech in enumerate(crew):
        db.add(JobAssignment(id=str(uuid4()), job_id=job.id.hex, tech_id=tech,
                             is_lead=(i == 0), assigned_at=_now() - timedelta(days=3)))
    db.commit()
    return job


def _visit(db, job, start, *, tech=TECH, status="scheduled", arrived=None,
           en_route=None) -> Appointment:
    a = Appointment(
        id=uuid4(), company_id=TENANT, job_id=job.id, customer_id=job.customer_id,
        tech_id=tech, title=job.title, start_at=start, end_at=start + timedelta(hours=8),
        status=status, arrived_at=arrived, en_route_at=en_route,
        created_at=_now() - timedelta(days=7),
    )
    db.add(a)
    db.commit()
    return a


def _day_two(db, **job_kw):
    """Day 1 (yesterday) worked and closed, day 2 (today) booked: the job's
    dispatch_status is still the on_site day 1 left behind."""
    job = _job(db, dispatch_status="on_site", **job_kw)
    now = _now()
    _visit(db, job, now - timedelta(days=1), status="completed",
           arrived=now - timedelta(days=1))
    today = _visit(db, job, now)
    return job, today


def _fresh(db, model, key):
    db.expire_all()
    return db.get(model, key)


def _open_timers(db, job) -> list[TimeEntry]:
    db.expire_all()
    return list(db.execute(select(TimeEntry).where(
        TimeEntry.job_id == job.id, TimeEntry.clock_out.is_(None),
    ).order_by(TimeEntry.clock_in)).scalars().all())


def _yesterdays_timer(client, db, job) -> str:
    """A timer opened through the real route, then moved to yesterday: the
    forgotten day nobody answered "No" for."""
    r = client.post(f"/api/mobile/jobs/{job.id.hex}/clock-in")
    assert r.status_code == 201, r.text
    entry_id = r.json()["entry_id"]
    db.execute(text("UPDATE time_entries SET clock_in = :t WHERE id = :id"),
               {"t": _now() - timedelta(days=1), "id": entry_id})
    db.commit()
    return entry_id


def _audit(db, action, job) -> list[AuditLog]:
    db.expire_all()
    return [a for a in db.execute(select(AuditLog).where(AuditLog.action == action))
            .scalars().all() if str(a.entity_id) in (job.id.hex, str(job.id))]


# ── B8: the timer of a forgotten day ─────────────────────────────────────


def test_b8_arrival_on_day_3_opens_a_new_timer_and_day_2s_no_closes_only_day_2s(ctx):
    client, db, _ = ctx
    job, _ = _day_two(db)
    old_id = _yesterdays_timer(client, db, job)

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")

    assert r.status_code == 200, r.text
    assert r.json()["auto_clock_in"] is True
    timers = _open_timers(db, job)
    assert len(timers) == 2
    assert str(timers[0].id) == old_id  # left open for its own day's "No"
    from gdx_dispatch.core.pay_periods import shop_day_of
    from gdx_dispatch.services.day_close import candidate_timers

    yesterday = shop_day_of(_now() - timedelta(days=1), "America/New_York")
    today = shop_day_of(_now(), "America/New_York")
    job = _fresh(db, Job, job.id)
    assert [str(t.id) for t in candidate_timers(db, job, yesterday, "America/New_York")] == [old_id]
    assert [str(t.id) for t in candidate_timers(db, job, today, "America/New_York")] == [
        str(timers[1].id)]


def test_b8_arrival_still_reuses_a_timer_opened_today(ctx):
    client, db, _ = ctx
    job, _ = _day_two(db)
    r = client.post(f"/api/mobile/jobs/{job.id.hex}/clock-in")
    assert r.status_code == 201

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")

    assert r.status_code == 200, r.text
    assert r.json()["auto_clock_in"] is False
    assert len(_open_timers(db, job)) == 1


def test_b8_clock_in_with_yesterdays_timer_open_opens_todays(ctx):
    client, db, _ = ctx
    job, _ = _day_two(db)
    old_id = _yesterdays_timer(client, db, job)

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/clock-in")

    assert r.status_code == 201, r.text
    assert r.json()["entry_id"] != old_id
    assert len(_open_timers(db, job)) == 2
    # and today's now blocks a second one, as before
    r = client.post(f"/api/mobile/jobs/{job.id.hex}/clock-in")
    assert r.status_code == 409
    assert r.json()["entry_id"] != old_id


def test_b8_stopping_todays_timer_does_not_surface_yesterdays(ctx):
    """Once today's timer is stopped, the toggle shows no running clock and
    Stop finds nothing: yesterday's timer waits for its own day's "No"."""
    client, db, _ = ctx
    job, _ = _day_two(db)
    old_id = _yesterdays_timer(client, db, job)
    assert client.post(f"/api/mobile/jobs/{job.id.hex}/clock-in").status_code == 201
    assert client.post(f"/api/mobile/jobs/{job.id.hex}/clock-out").status_code == 200

    clock = client.get(f"/api/mobile/job/{job.id.hex}").json()["clocks"]["job"]
    assert clock["running"] is False, clock
    assert clock["entry_id"] is None

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/clock-out")
    assert r.status_code == 404, r.text
    assert [str(t.id) for t in _open_timers(db, job)] == [old_id]


# ── B3 / B4: the phone on day 2 ──────────────────────────────────────────


def test_b3_b4_day_2_en_route_is_gated_by_todays_visit_and_stamps_it(ctx):
    client, db, _ = ctx
    job, today = _day_two(db)

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})

    assert r.status_code == 200, r.text  # was 400: on_site -> en_route
    assert r.json()["dispatch_status"] == "en_route"
    assert _fresh(db, Job, job.id).dispatch_status == "en_route"
    assert _fresh(db, Appointment, today.id).en_route_at is not None
    [row] = _audit(db, "en_route", job)
    assert row.details["visit_id"] == str(today.id)


def test_b4_en_route_keeps_an_earlier_en_route_stamp(ctx):
    client, db, _ = ctx
    job = _job(db, dispatch_status="en_route")
    earlier = _now() - timedelta(minutes=20)
    today = _visit(db, job, _now(), en_route=earlier)

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})

    assert r.status_code == 200, r.text
    assert _utc(_fresh(db, Appointment, today.id).en_route_at) == earlier


def test_b3_never_backwards_within_a_visit(ctx):
    client, db, _ = ctx
    job = _job(db, dispatch_status="on_site")
    today = _visit(db, job, _now(), arrived=_now())

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})

    assert r.status_code == 400
    assert _fresh(db, Appointment, today.id).en_route_at is None
    assert _fresh(db, Job, job.id).dispatch_status == "on_site"


def test_b3_without_a_visit_today_the_job_status_still_gates(ctx):
    client, db, _ = ctx
    job = _job(db, dispatch_status="on_site")
    _visit(db, job, _now() + timedelta(days=2))

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})

    assert r.status_code == 400


def test_b3_roll_up_keeps_on_site_while_a_crewmate_is_on_site(ctx):
    client, db, _ = ctx
    job = _job(db, dispatch_status="on_site", crew=(TECH, TECH2))
    _visit(db, job, _now(), tech=TECH2, arrived=_now())
    mine = _visit(db, job, _now())

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})

    assert r.status_code == 200, r.text
    assert r.json()["dispatch_status"] == "on_site"
    assert _fresh(db, Job, job.id).dispatch_status == "on_site"
    assert _fresh(db, Appointment, mine.id).en_route_at is not None


def test_b3_roll_up_ignores_closed_visits_and_other_days(ctx):
    """Yesterday's arrived-and-closed visit does not keep today at on_site."""
    client, db, _ = ctx
    job, today = _day_two(db)
    _visit(db, job, _now() + timedelta(days=1), tech=TECH2, en_route=_now())

    client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    assert _fresh(db, Job, job.id).dispatch_status == "en_route"

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    assert r.json()["dispatch_status"] == "on_site"
    assert _fresh(db, Appointment, today.id).arrived_at is not None


# ── today_visit on the job payload ───────────────────────────────────────


def test_today_visit_on_the_job_detail_payload(ctx):
    client, db, _ = ctx
    job, today = _day_two(db)

    body = client.get(f"/api/mobile/job/{job.id.hex}").json()
    assert body["job"]["today_visit"] == {
        "id": str(today.id), "state": "open", "en_route_at": None, "arrived_at": None,
    }

    client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    tv = client.get(f"/api/mobile/job/{job.id.hex}").json()["job"]["today_visit"]
    assert tv["state"] == "open" and tv["en_route_at"] is not None

    client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    tv = client.get(f"/api/mobile/job/{job.id.hex}").json()["job"]["today_visit"]
    assert tv["state"] == "on_site" and tv["arrived_at"] is not None


def test_today_visit_is_null_for_another_day_or_another_tech(ctx):
    client, db, be = ctx
    job = _job(db, crew=(TECH, TECH2))
    _visit(db, job, _now() + timedelta(days=1))
    _visit(db, job, _now(), tech=TECH2)

    body = client.get(f"/api/mobile/job/{job.id.hex}").json()
    assert body["job"]["today_visit"] is None

    be(TECH2_USER)
    tv = client.get(f"/api/mobile/job/{job.id.hex}").json()["job"]["today_visit"]
    assert tv is not None and tv["state"] == "open"


# ── B7: en route and arrival start the job ───────────────────────────────


@pytest.mark.parametrize("route", ["en-route", "arrived"])
def test_b7_en_route_and_arrival_start_a_scheduled_job(ctx, route):
    client, db, _ = ctx
    job = _job(db, stage="scheduled")
    _visit(db, job, _now())

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/{route}", json={})

    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert (j.lifecycle_stage, j.status) == ("in_progress", "In Progress")
    assert j.started_at is not None
    action = "en_route" if route == "en-route" else "arrived"
    [row] = _audit(db, action, job)
    assert row.details["job_started"] is True


@pytest.mark.parametrize("stage", ["estimate", "lead"])
def test_b7_driving_to_an_estimate_does_not_start_the_job(ctx, stage):
    client, db, _ = ctx
    job = _job(db, stage=stage)
    _visit(db, job, _now())

    r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})

    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert j.lifecycle_stage == stage
    assert j.started_at is None


def test_b7_a_started_job_is_not_restamped(ctx):
    client, db, _ = ctx
    started = _now() - timedelta(days=1)
    job, _ = _day_two(db, started_at=started)

    client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")

    j = _fresh(db, Job, job.id)
    assert _utc(j.started_at) == started
    assert j.lifecycle_stage == "in_progress"
    assert [r.details["job_started"] for r in _audit(db, "en_route", job)] == [False]


def test_b7_start_job_still_moves_the_stage_through_the_shared_writer(ctx):
    client, db, _ = ctx
    job = _job(db, stage="scheduled")
    _visit(db, job, _now())

    r = client.post(f"/api/jobs/{job.id}/start")

    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert (j.lifecycle_stage, j.status) == ("in_progress", "In Progress")
    assert j.started_at is not None
