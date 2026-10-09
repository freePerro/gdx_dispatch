"""Undo arrival (multi-day-jobs-plan §5.2a, *Arrival is always recorded*,
*Undo arrival*), pinned clause by clause against item 3 of *Tests: the whole
table, not case by case*.

Every undo goes through the real HTTP routes (``/api/appointments/{id}/
undo-arrival`` and ``/api/jobs/{id}/undo-arrival``) as a dispatcher. An
arrival is written either by the real routes (the mobile tap, office
Arrived, the PATCH) or — where the clause needs a tap at a chosen time on a
chosen day — by ``_seed_tap``, which runs the mobile route's own
``_arrival_visit`` / ``_stamp_arrival`` / ``stamp_tech_state`` and writes the
``arrived`` record in the route's payload shape (PR 1b, or the pre-PR-1b
shape: no ``tech_id``, no ``visit_id``, ``visit_moved`` without
``visit_end_from``).

Seeded taps sit two to three weeks in the past, in the shop's zone (the
default ``America/New_York``), so "later than the tap" holds for every audit
row the test then writes.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobAssignment,
    Technician,
)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.job_assignments import stamp_tech_state
from gdx_dispatch.services.visit_sync import (
    CANCELLED,
    CLOSED,
    OPEN,
    recompute_job_schedule,
    visit_state,
)
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-undo"
OFFICE = str(uuid4())
TECH_USER = str(uuid4())
TECH = str(uuid4())
TECH2_USER = str(uuid4())
TECH2 = str(uuid4())

NY = ZoneInfo("America/New_York")
D0 = datetime.now(NY).date() - timedelta(days=21)


def at(day: int, hh: int, mm: int = 0, ss: int = 0, us: int = 0) -> datetime:
    """A shop-local wall time on day ``D0 + day``, as UTC."""
    return datetime.combine(D0 + timedelta(days=day), time(hh, mm, ss, us), NY).astimezone(UTC)


def _utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ── fixture ───────────────────────────────────────────────────────────


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    # The tap route reads the job with raw SQL ``id = :job_id``: on SQLite a
    # dashed id never matches the 32-hex column, and a hex URL would key the
    # tap record, the crew stamp and the clock-in by the hex string, which
    # no client sends. The phone sends the dashed id, so the route is called
    # with it and only its two raw-SQL reads are given the hex form here.
    from uuid import UUID as _U

    _get_job = mobile_router._get_job
    monkeypatch.setattr(mobile_router, "_get_job",
                        lambda db, tenant_id, job_id: _get_job(db, tenant_id, _U(job_id).hex))
    monkeypatch.setattr(mobile_router, "_assert_job_access", lambda *a, **k: None)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.routers.appointments import router as appointments_router
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user
    from gdx_dispatch.routers.jobs import router as jobs_router

    caller: dict = {}

    app = FastAPI()
    app.include_router(jobs_router)
    app.include_router(appointments_router)
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller
    for key in ("jobs", "mobile"):
        app.dependency_overrides[require_module(key)] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "undo"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    def be(user_id: str, role: str) -> None:
        caller.clear()
        caller.update({"user_id": user_id, "sub": user_id, "tenant_id": TENANT, "role": role})

    db.add(Technician(id=TECH, company_id=TENANT, user_id=TECH_USER, active=True))
    db.add(Technician(id=TECH2, company_id=TENANT, user_id=TECH2_USER, active=True))
    db.commit()
    be(OFFICE, "dispatcher")
    yield TestClient(app, raise_server_exceptions=False), db, be
    db.close()
    engine.dispose()


# ── seeding ───────────────────────────────────────────────────────────


def _job(db, *, crew=(TECH,), dispatch_status="assigned", stage="scheduled", scheduled_at=None) -> Job:
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    job = Job(
        id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Install 16x7",
        description="", scheduled_at=scheduled_at or at(1, 9), status="Scheduled",
        priority="Normal", job_type="Install", lifecycle_stage=stage,
        assigned_to=crew[0] if crew else None, dispatch_status=dispatch_status,
        billing_status="unbilled", is_demo=False, is_return_visit=False,
    )
    db.add(job)
    db.flush()
    for i, tech in enumerate(crew):
        db.add(JobAssignment(
            id=str(uuid4()), job_id=str(job.id), tech_id=tech, is_lead=(i == 0),
            assigned_at=at(0, 7) + timedelta(minutes=i),
        ))
    db.commit()
    return job


def _visit(db, job, start, *, tech=TECH, status="scheduled", arrived=None, hours=8) -> Appointment:
    a = Appointment(
        id=uuid4(), company_id=TENANT, job_id=job.id, customer_id=job.customer_id,
        tech_id=tech, title=job.title, start_at=start, end_at=start + timedelta(hours=hours),
        status=status, arrived_at=arrived, created_at=at(0, 6),
    )
    db.add(a)
    db.commit()
    return a


def _seed_tap(db, job, when, *, tech=TECH, user=TECH_USER, old=False) -> str:
    """What ``mobile_job_arrived`` does at ``when``; returns the tap record id."""
    job = db.get(Job, job.id)
    job.dispatch_status = "on_site"
    if job.arrived_at is None:
        job.arrived_at = when
    appt = mobile_router._arrival_visit(db, job.id, tech, when)
    moved = stamped = None
    if appt is not None and appt.arrived_at is None:
        moved = mobile_router._stamp_arrival(db, appt, when)
        stamped = str(appt.id)
    recompute_job_schedule(db, job, user, "arrival")
    stamp_tech_state(db, job_id=str(job.id), tech_id=tech, state="arrived", when=when)
    details = {"auto_clock_in": False, "arrived_at": when.isoformat(),
               "lat": None, "lng": None, "accuracy": None}
    if old:
        if moved:  # the pre-PR-1b ``visit_moved``: no end, the job's old date
            details["visit_moved"] = {
                "visit_id": moved["visit_id"],
                "visit_start_from": moved["visit_start_from"],
                "job_scheduled_at_from": None,
            }
    else:
        details.update({"tech_id": tech, "visit_id": stamped, "visit_moved": moved})
    row = log_audit_event_sync(
        db, tenant_id=TENANT, user_id=user, action="arrived",
        entity_type="job", entity_id=str(job.id), details=details,
    )
    db.commit()
    return str(row.id)


# ── reading back ──────────────────────────────────────────────────────


def _fresh(db, model, key):
    db.expire_all()
    return db.get(model, key)


def _assignment(db, job, tech=TECH) -> JobAssignment:
    db.expire_all()
    return db.execute(select(JobAssignment).where(
        JobAssignment.job_id == str(job.id), JobAssignment.tech_id == tech,
    )).scalar_one()


def _audit_count(db) -> int:
    db.expire_all()
    return db.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def _undo_rows(db, job) -> list[AuditLog]:
    db.expire_all()
    return db.execute(select(AuditLog).where(
        AuditLog.action == "arrival_undone", AuditLog.entity_id == str(job.id),
    ).order_by(AuditLog.created_at)).scalars().all()


def _snapshot(db, job) -> tuple:
    """Everything an undo or a refused writer could touch."""
    db.expire_all()
    j = db.get(Job, job.id)
    visits = db.execute(select(Appointment).where(Appointment.job_id == job.id)
                        .order_by(Appointment.start_at, Appointment.id)).scalars().all()
    crew = db.execute(select(JobAssignment).where(JobAssignment.job_id == str(job.id))
                      .order_by(JobAssignment.tech_id)).scalars().all()
    return (
        (j.lifecycle_stage, j.status, j.dispatch_status, _utc(j.arrived_at), _utc(j.scheduled_at)),
        [(str(v.id), v.tech_id, v.status, _utc(v.start_at), _utc(v.end_at),
          _utc(v.arrived_at), v.deleted_at) for v in visits],
        [(c.tech_id, _utc(c.arrived_at)) for c in crew],
        _audit_count(db),
    )


def _undo_visit(client, v, reason="mis-tap", **kw):
    return client.post(f"/api/appointments/{v.id}/undo-arrival", json={"reason": reason, **kw})


def _undo_taps(client, job, tap_ids, tech=TECH, reason="mis-tap"):
    return client.post(f"/api/jobs/{job.id}/undo-arrival",
                       json={"tech_id": tech, "tap_record_ids": list(tap_ids), "reason": reason})


def _reasons(body) -> dict:
    return {n["field"]: n["reason"] for n in body["not_reverted"]}


# ── reason required ───────────────────────────────────────────────────


@pytest.mark.parametrize("method,route", [
    ("GET", "appt"), ("POST", "appt"), ("GET", "job"), ("POST", "job"),
])
def test_a_technician_is_refused_every_undo_route(ctx, method, route):
    client, db, be = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9))
    _seed_tap(db, job, at(1, 8, 14))
    before = _snapshot(db, job)
    be(TECH_USER, "technician")
    if route == "appt":
        url, body = f"/api/appointments/{v.id}/undo-arrival", {"reason": "mis-tap"}
    else:
        url, body = f"/api/jobs/{job.id}/undo-arrival", {"tech_id": TECH, "tap_record_ids": [], "reason": "mis-tap"}
    if method == "GET":
        r = client.get(url, params={"tech_id": TECH} if route == "job" else None)
    else:
        r = client.post(url, json=body)
    assert r.status_code == 403, r.text
    assert _snapshot(db, job) == before


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
def test_undo_on_a_visit_requires_a_reason_and_writes_nothing(ctx, body):
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9))
    _seed_tap(db, job, at(1, 8, 14))
    before = _snapshot(db, job)
    r = client.post(f"/api/appointments/{v.id}/undo-arrival", json=body)
    assert r.status_code == 422, r.text
    assert _snapshot(db, job) == before


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_undo_on_the_crew_row_requires_a_reason_and_writes_nothing(ctx, reason):
    client, db, _ = ctx
    job = _job(db)
    _visit(db, job, at(2, 9))
    _visit(db, job, at(3, 9))
    tap = _seed_tap(db, job, at(1, 8))  # A3
    before = _snapshot(db, job)
    body = {"tech_id": TECH, "tap_record_ids": [tap]}
    if reason is not None:
        body["reason"] = reason
    r = client.post(f"/api/jobs/{job.id}/undo-arrival", json=body)
    assert r.status_code == 422, r.text
    assert _snapshot(db, job) == before


# ── every source × open / completed / cancelled ───────────────────────

SOURCES = ["tap", "old_tap", "office_arrived", "patch", "status_only"]
FINISHES = ["open", "completed", "cancelled"]
EXPECTED_STATE = {"open": OPEN, "completed": CLOSED, "cancelled": CANCELLED}
AUDIT_SOURCE = {"tap": "tap", "old_tap": "tap", "office_arrived": "manual",
                "patch": "manual", "status_only": "status_only"}


@pytest.mark.parametrize("finish", FINISHES)
@pytest.mark.parametrize("source", SOURCES)
def test_undo_works_on_an_arrival_from_each_source_and_leaves_the_terms_state(ctx, source, finish):
    client, db, be = ctx
    if source == "status_only" and finish != "open":
        pytest.skip("a status-only row is status 'arrived': it cannot also be completed or cancelled")
    if source == "tap":  # the real route, now
        job = _job(db, scheduled_at=datetime.now(UTC))
        v = _visit(db, job, datetime.now(UTC) - timedelta(minutes=5))
        be(TECH_USER, "technician")
        r = client.post(f"/api/mobile/jobs/{job.id}/arrived")
        assert r.status_code == 200, r.text
        be(OFFICE, "dispatcher")
    else:
        job = _job(db)
        v = _visit(db, job, at(1, 9), status="arrived" if source == "status_only" else "scheduled")
        if source == "old_tap":
            _seed_tap(db, job, at(1, 8, 14), old=True)
        elif source == "office_arrived":
            r = client.post(f"/api/appointments/{v.id}/arrived", json={"arrived_at": at(1, 8, 30).isoformat()})
            assert r.status_code == 200, r.text
        elif source == "patch":
            r = client.patch(f"/api/appointments/{v.id}", json={"status": "arrived"})
            assert r.status_code == 200, r.text
    v = _fresh(db, Appointment, v.id)
    assert v.arrived_at is not None or source == "status_only"
    if finish != "open":
        r = client.post(f"/api/appointments/{v.id}/{'complete' if finish == 'completed' else 'cancel'}")
        assert r.status_code == 200, r.text

    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    v = _fresh(db, Appointment, v.id)
    assert v.arrived_at is None
    assert v.status != "arrived"
    assert visit_state(v) == EXPECTED_STATE[finish]
    row = _undo_rows(db, job)[-1]
    assert row.details["source"] == AUDIT_SOURCE[source]
    assert row.details["reason"] == "mis-tap"
    assert row.user_id == OFFICE
    assert row.details["visit_arrivals"][0]["visit_id"] == str(v.id)


# ── a tap record puts everything back ─────────────────────────────────


def _time_entries(db, job):
    db.expire_all()
    return db.execute(text(
        "SELECT id, clock_in, clock_out FROM time_entries WHERE job_id = :j ORDER BY id"
    ), {"j": str(job.id)}).all()


def test_undo_of_a_tap_puts_back_the_a2_move_reverts_every_stamp_and_leaves_the_time_entry(ctx):
    client, db, be = ctx
    later = (datetime.now(UTC) + timedelta(days=3)).replace(second=0, microsecond=0)
    job = _job(db, scheduled_at=later)
    v = _visit(db, job, later)
    end = _utc(v.end_at)
    be(TECH_USER, "technician")
    r = client.post(f"/api/mobile/jobs/{job.id}/arrived")
    assert r.status_code == 200, r.text
    be(OFFICE, "dispatcher")
    moved_start = _utc(_fresh(db, Appointment, v.id).start_at)
    assert moved_start != later  # A2 moved it onto today
    entries = _time_entries(db, job)
    assert len(entries) == 1 and entries[0].clock_out is None
    j = _fresh(db, Job, job.id)
    assert j.dispatch_status == "on_site" and j.arrived_at is not None
    tapped_at = _utc(j.arrived_at)

    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["not_reverted"] == []
    v = _fresh(db, Appointment, v.id)
    assert (_utc(v.start_at), _utc(v.end_at), v.arrived_at) == (later, end, None)
    assert _assignment(db, job).arrived_at is None
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"
    assert _utc(j.scheduled_at) == later  # invariant I, recomputed
    assert _time_entries(db, job) == entries  # the clock-in is not a dispatch action's
    changes = _undo_rows(db, job)[-1].details["changes"]
    assert _utc(datetime.fromisoformat(changes["visit.arrived_at"]["old"])) == tapped_at
    assert _utc(datetime.fromisoformat(changes["visit.start_at"]["old"])) == moved_start
    assert _utc(datetime.fromisoformat(changes["visit.start_at"]["new"])) == later
    assert "visit.end_at" in changes
    assert changes[f"assignment.{TECH}.arrived_at"]["new"] is None
    assert _utc(datetime.fromisoformat(changes["job.arrived_at"]["old"])) == tapped_at
    assert changes["job.dispatch_status"] == {"old": "on_site", "new": "assigned"}


# ── the crew row ──────────────────────────────────────────────────────


def test_crew_row_lists_only_this_techs_unmatched_taps(ctx):
    client, db, _ = ctx
    job = _job(db, crew=(TECH, TECH2))
    _visit(db, job, at(0, 9))
    _visit(db, job, at(2, 9))
    _visit(db, job, at(3, 9), tech=TECH2)
    matched = _seed_tap(db, job, at(0, 8))  # A1: stamps the day-0 visit
    unmatched = _seed_tap(db, job, at(1, 8))  # A3
    _seed_tap(db, job, at(1, 8, 30), tech=TECH2, user=TECH2_USER)  # another tech's
    r = client.get(f"/api/jobs/{job.id}/undo-arrival", params={"tech_id": TECH})
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["unmatched_taps"]] == [unmatched]
    assert matched not in [t["id"] for t in r.json()["unmatched_taps"]]


@pytest.mark.parametrize("old", [False, True], ids=["pr1b_record", "pre_pr1b_record"])
def test_crew_row_undoes_the_named_tap(ctx, old):
    client, db, _ = ctx
    job = _job(db)
    _visit(db, job, at(2, 9))
    _visit(db, job, at(3, 9))
    tap = _seed_tap(db, job, at(1, 8), old=old)  # A3
    r = _undo_taps(client, job, [tap])
    assert r.status_code == 200, r.text
    assert r.json()["tap_record_ids"] == [tap]
    assert _assignment(db, job).arrived_at is None
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None
    # Plan §5.2a item 3 says a pre-PR-1b record reports dispatch_status as
    # ``no_record``. The implementation (arrival_undo._job_side) recomputes
    # dispatch_status from the surviving arrivals whatever the record's age,
    # so both records revert it to ``assigned`` and report nothing. Pinned as
    # built; the deviation is with the maintainer.
    assert j.dispatch_status == "assigned"
    assert r.json()["not_reverted"] == []
    row = _undo_rows(db, job)[-1]
    assert row.details["tap_record_ids"] == [tap] and row.details["tech_id"] == TECH


@pytest.mark.parametrize("named", ["matched", "random", "other_tech"])
def test_crew_row_refuses_a_tap_outside_the_list_with_422_and_writes_nothing(ctx, named):
    client, db, _ = ctx
    job = _job(db, crew=(TECH, TECH2))
    _visit(db, job, at(0, 9))
    _visit(db, job, at(2, 9))
    _visit(db, job, at(3, 9), tech=TECH2)
    matched = _seed_tap(db, job, at(0, 8))
    listed = _seed_tap(db, job, at(1, 8))
    other = _seed_tap(db, job, at(1, 8, 30), tech=TECH2, user=TECH2_USER)
    stray = {"matched": matched, "random": str(uuid4()), "other_tech": other}[named]
    before = _snapshot(db, job)
    r = _undo_taps(client, job, [listed, stray])
    assert r.status_code == 422, r.text
    assert r.json()["tap_record_ids"] == [stray]
    assert _snapshot(db, job) == before


@pytest.mark.parametrize("taps", ["all_stamped_visits", "none"])
def test_crew_row_is_409_with_an_empty_list_and_writes_nothing(ctx, taps):
    client, db, _ = ctx
    job = _job(db)
    _visit(db, job, at(1, 9))
    tap = _seed_tap(db, job, at(1, 8, 14)) if taps == "all_stamped_visits" else str(uuid4())
    assert client.get(f"/api/jobs/{job.id}/undo-arrival",
                      params={"tech_id": TECH}).json()["unmatched_taps"] == []
    before = _snapshot(db, job)
    r = _undo_taps(client, job, [tap])
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "no_tap"
    assert _snapshot(db, job) == before


# ── double tap, later same-day taps ───────────────────────────────────


def test_a_double_tap_with_the_second_named_in_also_undo_clears_every_stamp(ctx):
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9))
    first = _seed_tap(db, job, at(1, 8, 14))
    second = _seed_tap(db, job, at(1, 8, 14, 1, 300000))  # stamped nothing
    preview = client.get(f"/api/appointments/{v.id}/undo-arrival").json()
    assert preview["tap"]["id"] == first
    assert [t["id"] for t in preview["later_taps"][first]] == [second]
    r = _undo_visit(client, v, also_undo=[second])
    assert r.status_code == 200, r.text
    assert sorted(r.json()["tap_record_ids"]) == sorted([first, second])
    assert _assignment(db, job).arrived_at is None
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"


def test_a_mistap_with_an_unnamed_later_tap_refills_to_it_then_undoing_that_clears_both(ctx):
    client, db, _ = ctx
    job = _job(db, scheduled_at=at(3, 9))
    v = _visit(db, job, at(3, 9))
    mistap = _seed_tap(db, job, at(1, 8))  # A2: moves the day-3 visit to day 1
    assert _utc(_fresh(db, Appointment, v.id).start_at) == at(1, 8)
    real = _seed_tap(db, job, at(1, 13))  # same day, stamps nothing
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    assert r.json()["tap_record_ids"] == [mistap]
    assert _reasons(r.json()) == {"visit.start_at": "crew_came", "dispatch_status": "other_arrival"}
    v2 = _fresh(db, Appointment, v.id)
    assert v2.arrived_at is None  # never restamped from another tap
    assert _utc(v2.start_at) == at(1, 8)  # left on the day the crew came
    assert _utc(_assignment(db, job).arrived_at) == at(1, 13)
    j = _fresh(db, Job, job.id)
    assert _utc(j.arrived_at) == at(1, 13) and j.dispatch_status == "on_site"

    # Later, the 13:00 tap turns out wrong too: the crew row.
    assert [t["id"] for t in client.get(
        f"/api/jobs/{job.id}/undo-arrival", params={"tech_id": TECH}).json()["unmatched_taps"]] == [real]
    r = _undo_taps(client, job, [real])
    assert r.status_code == 200, r.text
    assert _assignment(db, job).arrived_at is None
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"


def test_a_same_day_tap_that_stamped_another_visit_and_a_later_days_tap_are_never_listed(ctx):
    client, db, _ = ctx
    job = _job(db)
    morning = _visit(db, job, at(1, 8), hours=2)
    _visit(db, job, at(1, 13), hours=2)  # same-day return
    first = _seed_tap(db, job, at(1, 8, 5))  # stamps the morning visit
    other_visit = _seed_tap(db, job, at(1, 13, 5))  # A1 stamps the return visit
    next_day = _seed_tap(db, job, at(2, 8))  # A3, unmatched
    preview = client.get(f"/api/appointments/{morning.id}/undo-arrival").json()
    assert preview["tap"]["id"] == first
    assert preview["later_taps"][first] == []
    before = _snapshot(db, job)
    for stray in (other_visit, next_day):
        r = _undo_visit(client, morning, also_undo=[stray])
        assert r.status_code == 422, r.text
        assert r.json()["tap_record_ids"] == [stray]
        assert _snapshot(db, job) == before


# ── surviving arrivals, office arrivals ───────────────────────────────


def _a3_job(db, **kw):
    """Two visits on later days: a day-1 tap stamps nothing (A3)."""
    job = _job(db, scheduled_at=at(2, 9), **kw)
    v2 = _visit(db, job, at(2, 9))
    v3 = _visit(db, job, at(3, 9))
    return job, v2, v3


def test_after_an_a3_tap_is_undone_undoing_the_surviving_office_arrival_clears_the_job(ctx):
    client, db, _ = ctx
    job, v2, _ = _a3_job(db)
    tap = _seed_tap(db, job, at(1, 8))
    r = client.post(f"/api/appointments/{v2.id}/arrived", json={"arrived_at": at(2, 9, 10).isoformat()})
    assert r.status_code == 200, r.text
    r = _undo_taps(client, job, [tap])
    assert r.status_code == 200, r.text
    assert _reasons(r.json()) == {"dispatch_status": "other_arrival"}
    j = _fresh(db, Job, job.id)
    assert _utc(j.arrived_at) == at(2, 9, 10) and j.dispatch_status == "on_site"
    assert _assignment(db, job).arrived_at is None  # a manual time never refills it

    r = _undo_visit(client, v2)
    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"


def test_job_arrived_at_copied_from_an_office_arrival_later_corrected_still_goes(ctx):
    client, db, _ = ctx
    job, v2, _ = _a3_job(db)
    tap = _seed_tap(db, job, at(1, 8))
    client.post(f"/api/appointments/{v2.id}/arrived", json={"arrived_at": at(2, 9).isoformat()})
    assert _undo_taps(client, job, [tap]).status_code == 200
    assert _utc(_fresh(db, Job, job.id).arrived_at) == at(2, 9)  # copied from the office arrival
    r = client.patch(f"/api/appointments/{v2.id}", json={"arrived_at": at(2, 9, 30).isoformat()})
    assert r.status_code == 200, r.text
    r = _undo_visit(client, v2)
    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"


def test_a_mistapped_job_cancelled_undone_then_reactivated_without_a_tech_reads_assigned(ctx):
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9))
    _seed_tap(db, job, at(1, 8, 14))
    r = client.patch(f"/api/jobs/{job.id}", json={"lifecycle_stage": "cancelled"})
    assert r.status_code == 200, r.text
    v1 = _fresh(db, Appointment, v.id)
    assert v1.status == "cancelled" and v1.arrived_at is not None  # X1: CLOSED, arrival kept
    j = _fresh(db, Job, job.id)
    if j.lifecycle_stage != "cancelled":
        # update_job writes the stage with raw SQL that does not land on
        # SQLite (plan §5.2a X1); finish the cancel through the ORM.
        j.lifecycle_stage = "cancelled"
        db.commit()
    assert _fresh(db, Job, job.id).dispatch_status == "on_site"
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    assert visit_state(_fresh(db, Appointment, v.id)) == CANCELLED
    assert _fresh(db, Job, job.id).dispatch_status == "assigned"
    r = client.post(f"/api/jobs/{job.id}/reactivate", json={"reason": "customer called back"})
    assert r.status_code == 200, r.text
    j = _fresh(db, Job, job.id)
    assert j.lifecycle_stage != "cancelled"
    assert j.dispatch_status == "assigned"


def test_an_en_route_job_is_left_en_route(ctx):
    client, db, _ = ctx
    job = _job(db, dispatch_status="en_route")
    v = _visit(db, job, at(1, 9))
    client.post(f"/api/appointments/{v.id}/arrived", json={"arrived_at": at(1, 9, 5).isoformat()})
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    assert _fresh(db, Job, job.id).dispatch_status == "en_route"
    assert "job.dispatch_status" not in r.json()["changes"]
    assert "dispatch_status" not in _reasons(r.json())


def test_two_techs_a3_taps_undoing_one_leaves_the_job_on_the_other(ctx):
    client, db, _ = ctx
    job = _job(db, crew=(TECH, TECH2), scheduled_at=at(2, 9))
    _visit(db, job, at(2, 9))
    _visit(db, job, at(3, 9), tech=TECH2)
    mine = _seed_tap(db, job, at(1, 8))
    _seed_tap(db, job, at(1, 8, 30), tech=TECH2, user=TECH2_USER)
    r = _undo_taps(client, job, [mine])
    assert r.status_code == 200, r.text
    assert _reasons(r.json()) == {"dispatch_status": "other_arrival"}
    j = _fresh(db, Job, job.id)
    assert _utc(j.arrived_at) == at(1, 8, 30) and j.dispatch_status == "on_site"
    assert _assignment(db, job, TECH).arrived_at is None
    assert _utc(_assignment(db, job, TECH2).arrived_at) == at(1, 8, 30)


@pytest.mark.parametrize("day2", ["a1_unassigned_visit", "a3_no_visit", "office_only"])
def test_undoing_a_day1_tap_moves_the_assignment_stamp_to_the_day2_tap(ctx, day2):
    client, db, _ = ctx
    job = _job(db, scheduled_at=at(2, 9))
    if day2 == "a1_unassigned_visit":
        slot = _visit(db, job, at(2, 9), tech=None)
        _visit(db, job, at(3, 9))
    elif day2 == "a3_no_visit":
        _visit(db, job, at(3, 9))
        _visit(db, job, at(4, 9))
    else:
        slot = _visit(db, job, at(2, 9))
        _visit(db, job, at(3, 9))
    day1 = _seed_tap(db, job, at(1, 8))
    assert _fresh(db, Job, job.id).arrived_at is not None
    if day2 == "office_only":
        client.post(f"/api/appointments/{slot.id}/arrived", json={"arrived_at": at(2, 8, 45).isoformat()})
    else:
        _seed_tap(db, job, at(2, 8, 45))
        if day2 == "a1_unassigned_visit":
            assert _utc(_fresh(db, Appointment, slot.id).arrived_at) == at(2, 8, 45)
    r = _undo_taps(client, job, [day1])
    assert r.status_code == 200, r.text
    expected = None if day2 == "office_only" else at(2, 8, 45)
    assert _utc(_assignment(db, job).arrived_at) == expected
    assert _utc(_fresh(db, Job, job.id).arrived_at) == at(2, 8, 45)


@pytest.mark.parametrize("old", [False, True], ids=["pr1b_record", "pre_pr1b_record"])
def test_a_tap_whose_time_was_corrected_by_patch_is_still_matched_and_fully_reverted(ctx, old):
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9))
    tap = _seed_tap(db, job, at(1, 8, 14), old=old)
    r = client.patch(f"/api/appointments/{v.id}", json={"arrived_at": at(1, 8, 20).isoformat()})
    assert r.status_code == 200, r.text
    preview = client.get(f"/api/appointments/{v.id}/undo-arrival").json()
    assert preview["source"] == "tap" and preview["tap"]["id"] == tap
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    assert r.json()["tap_record_ids"] == [tap] and r.json()["not_reverted"] == []
    assert _fresh(db, Appointment, v.id).arrived_at is None
    assert _assignment(db, job).arrived_at is None
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"


@pytest.mark.parametrize("name_it", [True, False])
def test_a_pre_pr1b_visit_overwritten_by_office_arrived_lists_only_its_days_tap(ctx, name_it):
    client, db, _ = ctx
    job = _job(db)
    v1 = _visit(db, job, at(1, 9))
    v2 = _visit(db, job, at(2, 9))
    first = _seed_tap(db, job, at(1, 8, 14), old=True)  # stamps v1
    second = _seed_tap(db, job, at(2, 8, 10), old=True)  # stamps v2
    v2 = _fresh(db, Appointment, v2.id)
    assert _utc(v2.arrived_at) == at(2, 8, 10)
    v2.arrived_at = at(2, 8, 40)  # the pre-PR-1b Arrived button: no old value recorded
    db.commit()
    preview = client.get(f"/api/appointments/{v2.id}/undo-arrival").json()
    assert preview["tap"] is None
    assert [t["id"] for t in preview["unmatched_taps"]] == [second]
    r = _undo_visit(client, v2, **({"tap_record_id": second} if name_it else {}))
    assert r.status_code == 200, r.text
    assert _fresh(db, Appointment, v2.id).arrived_at is None
    if name_it:
        assert r.json()["tap_record_ids"] == [second]
        assert "tap" not in _reasons(r.json())
    else:
        assert r.json()["tap_record_ids"] == []
        assert _reasons(r.json())["tap"] == "no_record"
    # The first day's arrival and the stamps it wrote stay.
    assert _utc(_fresh(db, Appointment, v1.id).arrived_at) == at(1, 8, 14)
    assert _utc(_assignment(db, job).arrived_at) == at(1, 8, 14)
    assert _utc(_fresh(db, Job, job.id).arrived_at) == at(1, 8, 14)
    assert first not in r.json()["tap_record_ids"]


def test_a_tap_named_for_an_unmatched_visit_must_be_in_its_list(ctx):
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9))
    client.post(f"/api/appointments/{v.id}/arrived", json={"arrived_at": at(1, 9, 5).isoformat()})
    before = _snapshot(db, job)
    r = _undo_visit(client, v, tap_record_id=str(uuid4()))
    assert r.status_code == 422, r.text
    assert _snapshot(db, job) == before


def test_an_undone_tap_is_not_counted_by_a_later_undo(ctx):
    client, db, _ = ctx
    job, v2, _ = _a3_job(db)
    t1 = _seed_tap(db, job, at(1, 8))  # A3: unmatched
    _seed_tap(db, job, at(2, 8, 50))  # A1: stamps the day-2 visit
    assert _utc(_fresh(db, Appointment, v2.id).arrived_at) == at(2, 8, 50)
    assert _undo_taps(client, job, [t1]).status_code == 200
    assert _utc(_assignment(db, job).arrived_at) == at(2, 8, 50)
    assert client.get(f"/api/jobs/{job.id}/undo-arrival",
                      params={"tech_id": TECH}).json()["unmatched_taps"] == []
    # Were t1 still counted, both stamps would refill to day 1 08:00 here.
    r = _undo_visit(client, v2)
    assert r.status_code == 200, r.text
    assert _assignment(db, job).arrived_at is None
    j = _fresh(db, Job, job.id)
    assert j.arrived_at is None and j.dispatch_status == "assigned"
    before = _snapshot(db, job)
    r = _undo_taps(client, job, [t1])
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "no_tap"
    assert _snapshot(db, job) == before


# ── the move is skipped, never silently ───────────────────────────────


@pytest.mark.parametrize("why", ["edited_since", "double_book", "closed_visit", "closed_job", "crew_came"])
def test_the_move_is_skipped_and_reported_while_the_rest_is_undone(ctx, why):
    client, db, _ = ctx
    job = _job(db, scheduled_at=at(3, 9))
    v = _visit(db, job, at(3, 9))
    tap = _seed_tap(db, job, at(1, 10))  # A2: day 3 -> day 1 10:00
    assert _utc(_fresh(db, Appointment, v.id).start_at) == at(1, 10)
    stays_at = at(1, 10)
    if why == "edited_since":
        stays_at = at(1, 11)
        r = client.patch(f"/api/appointments/{v.id}", json={
            "start_at": stays_at.isoformat(), "end_at": (stays_at + timedelta(hours=8)).isoformat(),
        })
        assert r.status_code == 200, r.text
    elif why == "double_book":
        _visit(db, job, at(3, 13), hours=2)
    elif why == "closed_visit":
        assert client.post(f"/api/appointments/{v.id}/complete").status_code == 200
    elif why == "closed_job":
        j = _fresh(db, Job, job.id)
        j.lifecycle_stage = "cancelled"
        db.commit()
    elif why == "crew_came":
        _seed_tap(db, job, at(1, 14))
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    reason = {"closed_visit": "closed", "closed_job": "closed"}.get(why, why)
    assert _reasons(r.json())["visit.start_at"] == reason
    assert r.json()["tap_record_ids"] == [tap]
    v2 = _fresh(db, Appointment, v.id)
    assert _utc(v2.start_at) == stays_at and v2.arrived_at is None
    row = _undo_rows(db, job)[-1]
    assert {"field": "visit.start_at", "reason": reason} in row.details["not_reverted"]
    assert row.details["not_reverted"] == r.json()["not_reverted"]



def test_a_save_that_resends_the_same_times_is_not_an_edit(ctx):
    """The Appointments form sends start_at/end_at on every save. A notes-only
    save between the tap and the undo must not keep the visit on the tap day."""
    client, db, _ = ctx
    job = _job(db, scheduled_at=at(3, 9))
    v = _visit(db, job, at(3, 9))
    _seed_tap(db, job, at(1, 10))  # A2: day 3 -> day 1 10:00
    moved = _fresh(db, Appointment, v.id)
    r = client.patch(f"/api/appointments/{v.id}", json={
        "start_at": _utc(moved.start_at).isoformat(), "end_at": _utc(moved.end_at).isoformat(),
        "notes": "gate code 4411",
    })
    assert r.status_code == 200, r.text
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    assert r.json()["not_reverted"] == []
    v2 = _fresh(db, Appointment, v.id)
    assert _utc(v2.start_at) == at(3, 9) and v2.arrived_at is None
    assert _utc(_fresh(db, Job, job.id).scheduled_at) == at(3, 9)



def test_an_end_only_edit_after_the_tap_keeps_the_move(ctx):
    """The start still equals the tap, so only the audit trail can show the
    office changed this visit's time: the move stays, and is reported."""
    client, db, _ = ctx
    job = _job(db, scheduled_at=at(3, 9))
    v = _visit(db, job, at(3, 9))
    _seed_tap(db, job, at(1, 10))
    moved = _fresh(db, Appointment, v.id)
    new_end = _utc(moved.end_at) + timedelta(hours=3)
    r = client.patch(f"/api/appointments/{v.id}", json={
        "start_at": _utc(moved.start_at).isoformat(), "end_at": new_end.isoformat(),
    })
    assert r.status_code == 200, r.text
    r = _undo_visit(client, v)
    assert r.status_code == 200, r.text
    assert _reasons(r.json())["visit.start_at"] == "edited_since"
    v2 = _fresh(db, Appointment, v.id)
    assert (_utc(v2.start_at), _utc(v2.end_at), v2.arrived_at) == (at(1, 10), new_end, None)


# ── an old status-only "arrived" row is asked about, never lost ───────


def _status_only_job(db, stage="scheduled"):
    job = _job(db, stage=stage)
    old = _visit(db, job, at(1, 9), status="arrived")
    _visit(db, job, at(2, 9))
    return job, old


def _assert_needs_answer(r, old):
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "needs_answer"
    assert body["question"] == "status_only_arrival"
    assert str(old.id) in body["visit_ids"]


def test_a_job_cancel_holding_a_status_only_row_needs_an_answer_and_writes_nothing(ctx):
    client, db, _ = ctx
    job, old = _status_only_job(db)
    before = _snapshot(db, job)
    _assert_needs_answer(client.patch(f"/api/jobs/{job.id}", json={"lifecycle_stage": "cancelled"}), old)
    assert _snapshot(db, job) == before


def test_a_reactivate_holding_a_status_only_row_needs_an_answer_and_writes_nothing(ctx):
    client, db, _ = ctx
    job, old = _status_only_job(db, stage="cancelled")
    before = _snapshot(db, job)
    _assert_needs_answer(
        client.post(f"/api/jobs/{job.id}/reactivate", json={"reason": "customer called back"}), old,
    )
    assert _snapshot(db, job) == before


def test_the_row_cancel_on_a_status_only_row_needs_an_answer_and_writes_nothing(ctx):
    client, db, _ = ctx
    job, old = _status_only_job(db)
    before = _snapshot(db, job)
    _assert_needs_answer(client.post(f"/api/appointments/{old.id}/cancel"), old)
    assert _snapshot(db, job) == before


def test_a_patch_to_cancelled_on_a_status_only_row_needs_an_answer_and_writes_nothing(ctx):
    client, db, _ = ctx
    job, old = _status_only_job(db)
    before = _snapshot(db, job)
    _assert_needs_answer(client.patch(f"/api/appointments/{old.id}", json={"status": "cancelled"}), old)
    assert _snapshot(db, job) == before


# ── a tapped visit's tech (GDXA-383) ─────────────────────────────────


@pytest.mark.parametrize("tapper,status", [(TECH, 200), (TECH2, 409)], ids=["the_tapper", "someone_else"])
def test_an_unassigned_tapped_visit_takes_only_its_tapper(ctx, tapper, status):
    # A1 stamps an unassigned visit and leaves its tech empty; the tap is
    # matched only while the visit's tech is empty or the tapper's.
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9), tech=None)
    tap = _seed_tap(db, job, at(1, 8, 14))
    assert _fresh(db, Appointment, v.id).arrived_at is not None
    r = client.patch(f"/api/appointments/{v.id}", json={"tech_id": tapper})
    assert r.status_code == status, r.text
    if status == 409:
        assert r.json()["code"] == "visit_arrived"
    assert _fresh(db, Appointment, v.id).tech_id == (tapper if status == 200 else None)
    preview = client.get(f"/api/appointments/{v.id}/undo-arrival").json()
    assert preview["tap"] is not None and preview["tap"]["id"] == tap


def test_an_unassigned_visit_tapped_with_no_tech_cannot_be_given_one(ctx):
    # A tap by an account with no technician row stamps the unassigned visit
    # and carries no tech; any name breaks the match and strands the tap.
    client, db, _ = ctx
    job = _job(db)
    v = _visit(db, job, at(1, 9), tech=None)
    tap = _seed_tap(db, job, at(1, 8, 14), tech=None, user=OFFICE)
    matched = client.get(f"/api/appointments/{v.id}/undo-arrival").json()["tap"]
    assert matched["id"] == tap and not matched["tech_id"], matched
    r = client.patch(f"/api/appointments/{v.id}", json={"tech_id": TECH})
    assert r.status_code == 409 and r.json()["code"] == "visit_arrived", r.text
    assert _fresh(db, Appointment, v.id).tech_id is None
    assert client.get(f"/api/appointments/{v.id}/undo-arrival").json()["tap"]["id"] == tap


def test_a_tapped_visit_cannot_be_handed_to_another_tech(ctx):
    client, db, _ = ctx
    job = _job(db, crew=(TECH, TECH2))
    v = _visit(db, job, at(1, 9))
    tap = _seed_tap(db, job, at(1, 8, 14))
    r = client.patch(f"/api/appointments/{v.id}", json={"tech_id": TECH2, "notes": "swap"})
    assert r.status_code == 409 and r.json()["code"] == "visit_arrived", r.text
    assert _fresh(db, Appointment, v.id).tech_id == TECH
    assert client.get(f"/api/appointments/{v.id}/undo-arrival").json()["tap"]["id"] == tap


def _stranded(db):
    # TECH tapped; the old edit form then handed the visit to TECH2, which
    # left TECH's tap matching nothing (audit round 6 of GDXA-383).
    job = _job(db, crew=(TECH, TECH2))
    v = _visit(db, job, at(1, 9))
    tap = _seed_tap(db, job, at(1, 8, 14))
    row = db.get(Appointment, v.id)
    row.tech_id = TECH2
    db.commit()
    return job, v, tap


def test_a_stranded_visit_can_go_back_to_its_tapper(ctx):
    client, db, _ = ctx
    _, v, tap = _stranded(db)
    assert client.get(f"/api/appointments/{v.id}/undo-arrival").json()["tap"] is None
    r = client.patch(f"/api/appointments/{v.id}", json={"tech_id": TECH, "notes": "back"})
    assert r.status_code == 200, r.text
    assert _fresh(db, Appointment, v.id).tech_id == TECH
    assert client.get(f"/api/appointments/{v.id}/undo-arrival").json()["tap"]["id"] == tap


@pytest.mark.parametrize("new_tech", [str(uuid4()), None], ids=["a_third_tech", "no_tech"])
def test_a_stranded_visit_goes_only_to_its_tapper(ctx, new_tech):
    client, db, _ = ctx
    _, v, _ = _stranded(db)
    r = client.patch(f"/api/appointments/{v.id}", json={"tech_id": new_tech})
    assert r.status_code == 409 and r.json()["code"] == "visit_arrived", r.text
    assert _fresh(db, Appointment, v.id).tech_id == TECH2


def test_a_visit_cannot_take_a_tap_another_visit_holds(ctx):
    # An old record matches by time alone: TECH's old tap stamped Y at 08:14,
    # and TECH2's X carries a manual arrival at the same minute. Handing X to
    # TECH would make both visits claim one tap (GDXA-383 audit).
    client, db, _ = ctx
    job = _job(db, crew=(TECH, TECH2))
    y = _visit(db, job, at(1, 9))
    tap = _seed_tap(db, job, at(1, 8, 14), old=True)
    assert client.get(f"/api/appointments/{y.id}/undo-arrival").json()["tap"]["id"] == tap
    x = _visit(db, job, at(1, 9), tech=TECH2, arrived=_fresh(db, Appointment, y.id).arrived_at)
    r = client.patch(f"/api/appointments/{x.id}", json={"tech_id": TECH})
    assert r.status_code == 409 and r.json()["code"] == "visit_arrived", r.text
    assert _fresh(db, Appointment, x.id).tech_id == TECH2
    assert client.get(f"/api/appointments/{y.id}/undo-arrival").json()["tap"]["id"] == tap


@pytest.mark.parametrize("hh", [8, 10], ids=["sorts_first", "sorts_last"])
def test_an_unassigned_twin_of_a_held_tap_takes_only_the_tapper(ctx, hh):
    # Y holds TECH's old tap; unassigned Z carries the same arrival, so it
    # matches the tap by time too. Z may not be given a stranger whichever
    # visit sorts last (GDXA-383 audit round 2).
    client, db, _ = ctx
    job = _job(db, crew=(TECH, TECH2))
    y = _visit(db, job, at(1, 9))
    _seed_tap(db, job, at(1, 8, 14), old=True)
    z = _visit(db, job, at(1, hh), tech=None, arrived=_fresh(db, Appointment, y.id).arrived_at)
    r = client.patch(f"/api/appointments/{z.id}", json={"tech_id": TECH2})
    assert r.status_code == 409 and r.json()["code"] == "visit_arrived", r.text
    assert _fresh(db, Appointment, z.id).tech_id is None
