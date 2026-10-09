"""Every visit writer, over HTTP (multi-day-jobs-plan §5.2a, *Writers*).

``test_multi_day_visits.py`` pins the rows of the state table through the
three service calls; this file drives each WRITER ROUTE end to end through a
FastAPI TestClient against a real (ORM-built, in-memory SQLite) database:

* after every accepted write, invariant I is checked by an independent
  oracle computed from the rows (``_assert_invariant_i``) -- never by calling
  ``recompute_job_schedule`` -- and the audit actions the request wrote are
  read back by ``entity_id`` (both the dashed and the 32-hex spelling, since
  the mobile routes audit the path string they were handed);
* every refusal is sent against a snapshot of the jobs, appointments,
  job_assignments and audit_logs tables, and the snapshot must be identical
  afterwards (``_unchanged``).

Future days are fixed (November 2026, noon-ish UTC so nothing straddles the
shop's midnight); routes that read "today" (completion, mobile arrival, the
reorder) are seeded relative to now.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobAssignment,
    Technician,
)
from gdx_dispatch.services.visit_sync import job_visit_fields
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-writers"
OFFICE_USER = str(uuid4())
TECH_USER = str(uuid4())
T1 = str(uuid4())  # technician row of TECH_USER
T2 = str(uuid4())
T3 = str(uuid4())

DAY1 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # a Monday, 09:00 Chicago
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)
DAY4 = DAY1 + timedelta(days=3)

Env = namedtuple("Env", "client db cust be_office be_tech")


# ── harness ───────────────────────────────────────────────────────────


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    db.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-{TENANT}", "tid": TENANT},
    )
    db.commit()

    from gdx_dispatch.api import public_router as public_mod
    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.routers import appointments as appointments_mod
    from gdx_dispatch.routers import job_assignments as assignments_mod
    from gdx_dispatch.routers import jobs as jobs_mod
    from gdx_dispatch.routers import mobile as mobile_mod
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user

    caller: dict = {}

    app = FastAPI()
    for mod in (jobs_mod, assignments_mod, appointments_mod, mobile_mod, public_mod):
        app.include_router(mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller
    app.dependency_overrides[require_module("mobile")] = lambda: True

    async def _api_key(request: Request) -> dict:
        request.state.api_key_tenant_id = TENANT
        request.state.api_key_scopes = ["read:jobs", "write:jobs"]
        request.state.api_key_prefix = "gdx_live_tes"
        return {"tenant_id": TENANT, "scopes": ["write:jobs"], "key_prefix": "gdx_live_tes"}

    app.dependency_overrides[public_mod._require_api_key] = _api_key

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "writers"}
        request.state.tenant_id = TENANT
        request.state.user = dict(caller)
        return await call_next(request)

    def be(user_id: str, role: str) -> None:
        caller.clear()
        caller.update({"user_id": user_id, "sub": user_id, "tenant_id": TENANT, "role": role})

    db.add(Technician(id=T1, company_id=TENANT, user_id=TECH_USER, active=True))
    db.add(Technician(id=T2, company_id=TENANT, active=True))
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    db.commit()
    be(OFFICE_USER, "admin")

    yield Env(
        TestClient(app, raise_server_exceptions=True), db, cust,
        lambda: be(OFFICE_USER, "admin"), lambda: be(TECH_USER, "technician"),
    )
    db.close()
    engine.dispose()


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _job(env, scheduled_at=DAY1, *, assigned_to=T1, stage="scheduled", crew=(), **kw) -> Job:
    job = Job(
        id=uuid4(), company_id=TENANT, customer_id=env.cust.id, title="Install 16x7",
        description="", scheduled_at=scheduled_at, status=stage.title(),
        priority="Normal", job_type="Install", lifecycle_stage=stage,
        assigned_to=assigned_to, dispatch_status="assigned", billing_status="unbilled",
        is_demo=False, is_return_visit=False, created_at=DAY1 - timedelta(days=30),
        updated_at=DAY1 - timedelta(days=30), **kw,
    )
    env.db.add(job)
    env.db.flush()
    for i, tech in enumerate(crew):
        env.db.add(JobAssignment(
            id=str(uuid4()), job_id=str(job.id), tech_id=tech, is_lead=(i == 0),
            assigned_at=DAY1 - timedelta(days=30) + timedelta(minutes=i),
        ))
    env.db.commit()
    return job


def _visit(env, job, start, tech=T1, status="scheduled", arrived=None, hours=8) -> Appointment:
    # Seeded with what E1 would copy (the job's title and customer), so a
    # crew change has no field drift to correct and its audit list is exact.
    fields = job_visit_fields(env.db, job) if job is not None else {"title": "Install 16x7"}
    fields.setdefault("customer_id", env.cust.id)
    a = Appointment(
        id=uuid4(), company_id=TENANT, job_id=job.id if job is not None else None,
        tech_id=tech, **fields,
        start_at=start, end_at=start + timedelta(hours=hours), status=status,
        arrived_at=arrived, created_at=DAY1 - timedelta(days=30),
    )
    env.db.add(a)
    env.db.commit()
    return a


def _visits(db, job_id) -> list[Appointment]:
    return db.execute(
        select(Appointment).where(Appointment.job_id == job_id, Appointment.deleted_at.is_(None))
        .order_by(Appointment.start_at, Appointment.tech_id)
    ).scalars().all()


def _layout(db, job_id) -> list[tuple]:
    db.expire_all()
    return [(v.tech_id, _utc(v.start_at), v.status) for v in _visits(db, job_id)]


def _assert_invariant_i(db, job_id, *, constrained=True) -> Job:
    """Invariant I from the rows alone: a live job (stage not completed or
    cancelled) holding a Current visit (not completed, not cancelled -- an
    arrived one included) sits exactly on the earliest Current start.

    ``constrained`` states which branch the test expects, so a test cannot
    pass by accident on the unconstrained one."""
    db.expire_all()
    job = db.get(Job, job_id)
    stage = (job.lifecycle_stage or "").lower()
    current = [
        v for v in _visits(db, job.id)
        if (v.status or "").lower() not in ("completed", "cancelled")
    ]
    applies = stage not in ("completed", "cancelled") and bool(current)
    assert applies == constrained, (stage, [(v.tech_id, v.start_at, v.status) for v in current])
    if applies:
        earliest = min(_utc(v.start_at) for v in current)
        assert _utc(job.scheduled_at) == earliest, (job.scheduled_at, earliest)
    return job


def _audit_ids(db) -> set:
    return set(db.execute(select(AuditLog.id)).scalars().all())


def _new_audit(db, before: set, *entity_ids) -> list[AuditLog]:
    keys = set()
    for e in entity_ids:
        u = e if isinstance(e, UUID) else UUID(str(e))
        keys |= {str(u), u.hex}
    rows = db.execute(select(AuditLog).where(AuditLog.entity_id.in_(keys))).scalars().all()
    return [r for r in rows if r.id not in before]


def _actions(db, before: set, *entity_ids) -> list[str]:
    return sorted(r.action for r in _new_audit(db, before, *entity_ids))


_TABLES = ("jobs", "appointments", "job_assignments", "audit_logs")


def _snapshot(db) -> dict:
    db.flush()  # a refused route's unflushed ORM writes must show too
    return {
        t: sorted(repr(tuple(r)) for r in db.execute(text(f"SELECT * FROM {t}")).all())  # noqa: S608 -- fixed table names
        for t in _TABLES
    }


def _refused(env, method, path, body, status, code=None, **expect):
    """Send a request that must be refused; nothing may change."""
    env.db.commit()
    before = _snapshot(env.db)
    r = env.client.request(method, path, json=body)
    assert r.status_code == status, r.text
    if code is not None:
        assert r.json().get("code") == code, r.json()
    for key, value in expect.items():
        assert r.json().get(key) == value, r.json()
    assert _snapshot(env.db) == before
    return r.json()


# ── routers/jobs.py ───────────────────────────────────────────────────


def test_create_job_books_the_tech_on_the_date(env):
    before = _audit_ids(env.db)
    r = env.client.post("/api/jobs", json={
        "title": "Install 16x7", "customer_id": str(env.cust.id),
        "scheduled_at": DAY1.isoformat(), "assigned_tech_id": T1, "job_type": "Install",
    })
    assert r.status_code in (200, 201), r.text
    job_id = UUID(r.json()["id"])
    assert _layout(env.db, job_id) == [(T1, DAY1, "scheduled")]
    _assert_invariant_i(env.db, job_id)
    assert "visit_added" in _actions(env.db, before, job_id)


def test_update_job_date_moves_the_visit(env):
    job = _job(env)
    _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    target = DAY1 + timedelta(hours=2)
    r = env.client.patch(f"/api/jobs/{job.id}", json={"scheduled_at": target.isoformat()})
    assert r.status_code == 200, r.text
    assert _layout(env.db, job.id) == [(T1, target, "scheduled")]
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == target
    assert _actions(env.db, before, job.id) == ["job_updated", "visit_moved"]


def test_update_job_crew_add_books_the_new_tech(env):
    job = _job(env)
    _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/jobs/{job.id}", json={"assigned_tech_ids": [T1, T2]})
    assert r.status_code == 200, r.text
    assert _layout(env.db, job.id) == sorted([(T1, DAY1, "scheduled"), (T2, DAY1, "scheduled")],
                                             key=lambda t: (t[1], t[0]))
    _assert_invariant_i(env.db, job.id)
    assert _actions(env.db, before, job.id) == ["job_updated", "visit_added"]



@pytest.mark.parametrize("save", ["crew_add", "date_from_none", "assignment_post"])
def test_a_visit_booked_by_a_date_or_crew_save_carries_the_jobs_customer(env, save):
    """Job Detail's schedule save sends only scheduled_at and assigned_tech_ids:
    the visit it books is still the job's customer's, not a blank one."""
    if save == "date_from_none":
        job = _job(env, scheduled_at=None)
        r = env.client.patch(f"/api/jobs/{job.id}", json={"scheduled_at": DAY1.isoformat()})
    else:
        job = _job(env, crew=(T1,))
        _visit(env, job, DAY1)
        if save == "crew_add":
            r = env.client.patch(f"/api/jobs/{job.id}", json={"assigned_tech_ids": [T1, T2]})
        else:
            r = env.client.post(f"/api/jobs/{job.id}/assignments", json={"tech_id": T2})
    assert r.status_code in (200, 201), r.text
    env.db.expire_all()
    rows = env.db.execute(select(Appointment).where(
        Appointment.job_id == job.id, Appointment.deleted_at.is_(None),
    )).scalars().all()
    assert rows
    assert {(a.customer_id, a.customer_name) for a in rows} == {(env.cust.id, "Acme")}


def test_update_job_cancel_retires_the_open_visits(env):
    job = _job(env)
    a = _visit(env, job, DAY1)
    _visit(env, job, DAY2)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/jobs/{job.id}", json={"lifecycle_stage": "cancelled"})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(Job, job.id).status == "Cancelled"
    # X1 retires both open days; with no Current visit, I is unconstrained.
    assert _layout(env.db, job.id) == []
    assert env.db.get(Appointment, a.id).deleted_at is not None
    _assert_invariant_i(env.db, job.id, constrained=False)
    acts = _actions(env.db, before, job.id)
    assert acts.count("visit_retired") == 2 and "job_updated" in acts


def test_start_job_hands_the_unassigned_visit_to_the_starter(env):
    env.db.add(Technician(id=T3, company_id=TENANT, user_id=OFFICE_USER, active=True))
    job = _job(env, assigned_to=None)
    _visit(env, job, DAY1, tech=None)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/start", json={})
    assert r.status_code == 200, r.text
    assert _layout(env.db, job.id) == [(T3, DAY1, "scheduled")]
    job = _assert_invariant_i(env.db, job.id)
    assert job.lifecycle_stage == "in_progress"
    assert "visit_reassigned" in _actions(env.db, before, job.id)


def test_start_job_recomputes_a_stale_date(env):
    job = _job(env, scheduled_at=DAY3)  # stale: its only visit is on DAY1
    _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/start", json={})
    assert r.status_code == 200, r.text
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY1
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed", "job_started"]


def _finish_seed(env):
    now = _now()
    job = _job(env, scheduled_at=now)
    today = _visit(env, job, now, arrived=now)
    later = _visit(env, job, DAY3)
    return job, today, later


@pytest.mark.parametrize("path, body, action", [
    ("complete", {}, "job_completed"),
    ("close-without-work", {"reason": "duplicate of another job"}, "job_closed_without_work"),
    ("closeout", {"parts": [], "hours": 2.5, "no_parts_used": True}, None),
])
def test_finishing_closes_today_and_retires_later_days(env, path, body, action):
    job, today, later = _finish_seed(env)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/{path}", json=body)
    assert r.status_code in (200, 201), r.text
    env.db.expire_all()
    assert env.db.get(Appointment, today.id).status == "completed"
    assert env.db.get(Appointment, today.id).arrived_at is not None
    later_row = env.db.get(Appointment, later.id)
    assert later_row.deleted_at is not None
    job = _assert_invariant_i(env.db, job.id, constrained=False)
    assert job.lifecycle_stage == "completed"
    acts = _actions(env.db, before, job.id)
    assert "visit_closed" in acts and "visit_retired" in acts
    if action:
        assert action in acts


def test_uncomplete_books_the_new_date(env):
    now = _now()
    job = _job(env, scheduled_at=now, stage="completed", completed_at=now)
    _visit(env, job, now, status="completed", arrived=now)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/uncomplete",
                        json={"reason": "closed the wrong job", "scheduled_at": DAY2.isoformat()})
    assert r.status_code == 200, r.text
    assert (T1, DAY2, "scheduled") in _layout(env.db, job.id)
    job = _assert_invariant_i(env.db, job.id)
    assert _utc(job.scheduled_at) == DAY2
    acts = _actions(env.db, before, job.id)
    assert "visit_added" in acts and "job_uncompleted" in acts


def test_reactivate_books_the_new_date(env):
    job = _job(env, scheduled_at=DAY1, stage="cancelled")
    _visit(env, job, DAY1, status="cancelled")
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/reactivate",
                        json={"reason": "customer called back", "scheduled_at": DAY2.isoformat()})
    assert r.status_code == 200, r.text
    assert (T1, DAY2, "scheduled") in _layout(env.db, job.id)
    job = _assert_invariant_i(env.db, job.id)
    assert _utc(job.scheduled_at) == DAY2
    acts = _actions(env.db, before, job.id)
    assert "visit_added" in acts and "job_reactivated" in acts
    # The trail keeps the date the job had, not the one it was given.
    (row,) = [a for a in _new_audit(env.db, before, job.id) if a.action == "job_reactivated"]
    assert row.details["prior_scheduled_at"].startswith("2026-11-02T15:00")


def test_reactivate_answered_no_keeps_the_typed_date(env):
    # "No" to the rebook question books nothing on that day; the date the
    # office typed still stands -- only a request with no date is cleared.
    job = _job(env, scheduled_at=DAY1, stage="cancelled")
    _visit(env, job, DAY1, status="completed", arrived=DAY1)
    typed = DAY1 + timedelta(hours=1)
    r = env.client.post(f"/api/jobs/{job.id}/reactivate", json={
        "reason": "customer called back", "scheduled_at": typed.isoformat(),
        "rebook_closed_day": False,
    })
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle_stage"] == "scheduled"
    assert _utc(env.db.get(Job, job.id).scheduled_at) == typed
    assert [s for _, _, s in _layout(env.db, job.id)] == ["completed"]  # nothing booked


def test_reactivate_without_a_date_comes_back_as_a_service_call(env):
    # Plan §5.2a: "re-open with and without a date". Cancelling retired the
    # visit, so the old date has nothing behind it: the job must not read
    # Scheduled with an empty board (Doug ruled 2026-10-05).
    job = _job(env, scheduled_at=DAY1, stage="cancelled")
    _visit(env, job, DAY1, status="cancelled")
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/reactivate", json={"reason": "customer called back"})
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle_stage"] == "service_call"
    assert r.json()["scheduled_at"] is None
    assert [s for _, _, s in _layout(env.db, job.id)] == ["cancelled"]  # nothing booked
    job = _assert_invariant_i(env.db, job.id, constrained=False)
    assert job.lifecycle_stage == "service_call" and job.scheduled_at is None
    rows = _new_audit(env.db, before, job.id)
    reactivated = [a for a in rows if a.action == "job_reactivated"]
    assert len(reactivated) == 1
    assert reactivated[0].details["prior_scheduled_at"].startswith("2026-11-02T15:00")


def test_a_dated_return_visit_is_booked_on_its_date(env):
    # The callback a dispatcher schedules must be on the board: on main the
    # old sync booked it on the next edit; nothing else would on this branch.
    parent = _job(env, scheduled_at=DAY1, stage="completed")
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{parent.id}/spawn-return-visit",
                        json={"reason": "door still noisy", "scheduled_at": DAY3.isoformat(),
                              "assigned_to": T2})
    assert r.status_code == 201, r.text
    child_id = UUID(r.json()["id"])
    assert _layout(env.db, child_id) == [(T2, DAY3, "scheduled")]
    child = _assert_invariant_i(env.db, child_id)
    assert child.lifecycle_stage == "scheduled"
    row = _visits(env.db, child_id)[0]
    assert (row.customer_id, row.customer_name) == (env.cust.id, "Acme")
    acts = _actions(env.db, before, child_id)
    assert "visit_added" in acts and "return_visit_spawned" in acts


def test_an_undated_return_visit_books_nothing(env):
    parent = _job(env, scheduled_at=DAY1, stage="completed")
    r = env.client.post(f"/api/jobs/{parent.id}/spawn-return-visit", json={"reason": "check later"})
    assert r.status_code == 201, r.text
    child_id = UUID(r.json()["id"])
    assert _layout(env.db, child_id) == []
    assert env.db.get(Job, child_id).lifecycle_stage == "service_call"


# ── routers/job_assignments.py ────────────────────────────────────────


def test_assignment_post_books_the_added_tech(env):
    job = _job(env, crew=(T1,))
    _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/jobs/{job.id}/assignments", json={"tech_id": T2})
    assert r.status_code == 201, r.text
    assert {(t, s) for t, s, _ in _layout(env.db, job.id)} == {(T1, DAY1), (T2, DAY1)}
    _assert_invariant_i(env.db, job.id)
    assert _actions(env.db, before, job.id) == ["visit_added"]
    assert _actions(env.db, before, r.json()["id"]) == ["assign"]


def test_assignment_delete_retires_the_current_day_and_moves_the_job(env):
    # Mutant target: without recompute in _sync_crew_visits the job stays on
    # DAY1, which no Current visit holds any more.
    job = _job(env, crew=(T1, T2))
    _visit(env, job, DAY1, tech=T1)
    _visit(env, job, DAY2, tech=T2)
    row = env.db.execute(select(JobAssignment).where(
        JobAssignment.job_id == str(job.id), JobAssignment.tech_id == T1)).scalar_one()
    before = _audit_ids(env.db)
    r = env.client.delete(f"/api/jobs/{job.id}/assignments/{row.id}")
    assert r.status_code == 200, r.text
    assert _layout(env.db, job.id) == [(T2, DAY2, "scheduled")]
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY2
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed", "visit_retired"]
    assert _actions(env.db, before, row.id) == ["unassign"]


# ── routers/mobile.py ─────────────────────────────────────────────────


def test_mobile_reorder_moves_each_job_onto_its_new_slot(env):
    base = datetime.combine(datetime.now(UTC).date(), time(1, 0), tzinfo=UTC)
    early, late = base, base + timedelta(hours=2)
    job_a = _job(env, scheduled_at=early)
    job_b = _job(env, scheduled_at=late)
    a = _visit(env, job_a, early, hours=1)
    b = _visit(env, job_b, late, hours=1)
    env.be_tech()
    before = _audit_ids(env.db)
    r = env.client.post("/api/mobile/today/reorder",
                        json={"appointment_ids": [str(b.id), str(a.id)]})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert _utc(_assert_invariant_i(env.db, job_a.id).scheduled_at) == late
    assert _utc(_assert_invariant_i(env.db, job_b.id).scheduled_at) == early
    assert _actions(env.db, before, job_a.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, job_b.id) == ["job_schedule_recomputed"]


def test_mobile_arrival_on_the_visit_day_stamps_it_and_keeps_the_date(env):
    now = _now()
    job = _job(env, scheduled_at=now)
    v = _visit(env, job, now)
    env.be_tech()
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(Appointment, v.id).arrived_at is not None
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == now
    rows = [x for x in _new_audit(env.db, before, job.id) if x.action == "arrived"]
    assert len(rows) == 1 and rows[0].details["visit_id"] == str(v.id)
    assert rows[0].details["tech_id"] == T1


def test_mobile_early_arrival_moves_the_only_visit_and_the_job(env):
    job = _job(env, scheduled_at=DAY1)
    v = _visit(env, job, DAY1)
    env.be_tech()
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    env.db.expire_all()
    row = env.db.get(Appointment, v.id)
    # A2 moves the visit to the arrival, to the minute.
    assert row.arrived_at is not None
    assert _utc(row.start_at) == _utc(row.arrived_at).replace(second=0, microsecond=0)
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == _utc(row.start_at)
    acts = _actions(env.db, before, job.id)
    assert "arrived" in acts and "job_schedule_recomputed" in acts


# ── routers/appointments.py ───────────────────────────────────────────


def _two_day_job(env, *, scheduled_at=DAY1):
    job = _job(env, scheduled_at=scheduled_at)
    d1 = _visit(env, job, DAY1)
    d2 = _visit(env, job, DAY2)
    return job, d1, d2


def test_appointment_post_an_earlier_visit_moves_the_job(env):
    job = _job(env, scheduled_at=DAY2)
    _visit(env, job, DAY2)
    before = _audit_ids(env.db)
    r = env.client.post("/api/appointments", json={
        "job_id": str(job.id), "tech_id": T2, "title": "Install 16x7",
        "start_at": DAY1.isoformat(), "end_at": (DAY1 + timedelta(hours=8)).isoformat(),
    })
    assert r.status_code == 201, r.text
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY1
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, r.json()["id"]) == ["appointment_created"]


def test_appointment_patch_moving_day_one_moves_the_job(env):
    job, d1, _ = _two_day_job(env)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/appointments/{d1.id}", json={
        "start_at": DAY3.isoformat(), "end_at": (DAY3 + timedelta(hours=8)).isoformat(),
    })
    assert r.status_code == 200, r.text
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY2
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, d1.id) == ["appointment_updated"]


def test_appointment_patch_job_id_change_recomputes_both_jobs(env):
    job_a, d1, _ = _two_day_job(env)
    job_b = _job(env, scheduled_at=DAY3)
    _visit(env, job_b, DAY3)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/appointments/{d1.id}", json={"job_id": str(job_b.id)})
    assert r.status_code == 200, r.text
    assert _utc(_assert_invariant_i(env.db, job_a.id).scheduled_at) == DAY2
    assert _utc(_assert_invariant_i(env.db, job_b.id).scheduled_at) == DAY1
    assert _actions(env.db, before, job_a.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, job_b.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, d1.id) == ["appointment_updated"]


def test_appointment_delete_day_one_moves_the_job(env):
    job, d1, _ = _two_day_job(env)
    before = _audit_ids(env.db)
    r = env.client.delete(f"/api/appointments/{d1.id}")
    assert r.status_code == 204, r.text
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY2
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, d1.id) == ["appointment_deleted"]


def test_job_delete_soft_deletes_its_visits_and_no_others(env):
    # GDXA-382: the cascade was raw SQL binding the dashed path id against a
    # Uuid column, which SQLite stores as 32 dashless hex — it matched nothing
    # here, so a deleted job's visits stayed live on the Appointments page.
    job, d1, d2 = _two_day_job(env)
    other = _job(env, scheduled_at=DAY3)
    keep = _visit(env, other, DAY3)
    before = _audit_ids(env.db)
    r = env.client.delete(f"/api/jobs/{job.id}")
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert d1.deleted_at is not None and d2.deleted_at is not None
    assert _utc(d1.updated_at) == _utc(d1.deleted_at)
    assert keep.deleted_at is None
    assert env.db.get(Job, job.id).deleted_at is not None
    assert _actions(env.db, before, job.id) == ["job_deleted"]


def test_assignment_delete_moves_the_primary_tech(env):
    # GDXA-382, same class: `_recompute_primary` bound the dashed job id in a
    # raw UPDATE, so on SQLite the removed lead stayed `Job.assigned_to`.
    job = _job(env, crew=(T1, T2))
    row = env.db.execute(select(JobAssignment).where(
        JobAssignment.job_id == str(job.id), JobAssignment.tech_id == T1)).scalar_one()
    r = env.client.delete(f"/api/jobs/{job.id}/assignments/{row.id}")
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(Job, job.id).assigned_to == T2


def test_job_list_filters_by_customer(env):
    # GDXA-382, same class: `j.customer_id = :customer_id` bound the dashed
    # query string, so on SQLite the filter returned no jobs at all.
    mine = _job(env)
    other_cust = Customer(id=uuid4(), name="Other", phone="556", email="a@x.com",
                          address="12 Main", company_id=TENANT)
    env.db.add(other_cust)
    env.db.commit()
    theirs = _job(env)
    theirs.customer_id = other_cust.id
    env.db.commit()
    r = env.client.get("/api/jobs", params={"customer_id": str(env.cust.id)})
    assert r.status_code == 200, r.text
    assert [UUID(i["id"]) for i in r.json()["items"]] == [mine.id]
    r = env.client.get("/api/jobs", params={"customer_id": "not-a-uuid"})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@pytest.mark.parametrize("verb, action, status", [
    ("confirm", "appointment_confirmed", "confirmed"),
    ("on-my-way", "appointment_en_route", "en_route"),
    ("arrived", "appointment_arrived", "arrived"),
])
def test_appointment_status_verbs_recompute_a_stale_job(env, verb, action, status):
    # The job's stored date is stale (DAY4): the verb must put it back on DAY1.
    job, d1, _ = _two_day_job(env, scheduled_at=DAY4)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/appointments/{d1.id}/{verb}", json={})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(Appointment, d1.id).status == status
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY1
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, d1.id) == [action]


@pytest.mark.parametrize("verb, action, status", [
    ("complete", "appointment_completed", "completed"),
    ("cancel", "appointment_cancelled", "cancelled"),
])
def test_appointment_closing_day_one_moves_the_job(env, verb, action, status):
    job, d1, _ = _two_day_job(env)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/appointments/{d1.id}/{verb}", json={})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(Appointment, d1.id).status == status
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == DAY2
    assert _actions(env.db, before, job.id) == ["job_schedule_recomputed"]
    assert _actions(env.db, before, d1.id) == [action]


# ── api/public_router.py ──────────────────────────────────────────────


def test_public_patch_date_moves_the_visit(env):
    job = _job(env)
    _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    target = DAY1 + timedelta(hours=3)
    r = env.client.patch(f"/api/v1/jobs/{job.id}", json={"scheduled_at": target.isoformat()})
    assert r.status_code == 200, r.text
    assert _layout(env.db, job.id) == [(T1, target, "scheduled")]
    assert _utc(_assert_invariant_i(env.db, job.id).scheduled_at) == target
    assert _actions(env.db, before, job.id) == ["job_updated", "visit_moved"]


def test_public_patch_cancel_retires_the_open_visits(env):
    job = _job(env)
    _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/v1/jobs/{job.id}", json={"status": "Canceled"})
    assert r.status_code == 200, r.text
    assert _layout(env.db, job.id) == []
    job = _assert_invariant_i(env.db, job.id, constrained=False)
    assert job.lifecycle_stage == "cancelled"
    assert _actions(env.db, before, job.id) == ["job_updated", "visit_retired"]



# ── refusals: nothing written ─────────────────────────────────────────


def test_refused_r1_onto_booked_day(env):
    job, _, _ = _two_day_job(env)
    _refused(env, "PATCH", f"/api/jobs/{job.id}", {"scheduled_at": DAY2.isoformat()},
             409, "onto_booked_day")
    _refused(env, "PATCH", f"/api/v1/jobs/{job.id}", {"scheduled_at": DAY3.isoformat()},
             409, "onto_booked_day")


def test_refused_r2_double_booked(env):
    job = _job(env)
    _visit(env, job, DAY1)
    _visit(env, job, DAY3, status="completed", arrived=DAY3)
    _refused(env, "PATCH", f"/api/jobs/{job.id}", {"scheduled_at": DAY3.isoformat()},
             409, "double_booked")


def test_refused_r3_two_open_visits(env):
    job = _job(env)
    _visit(env, job, DAY1, hours=2)
    _visit(env, job, DAY1 + timedelta(hours=4), hours=2)
    _refused(env, "PATCH", f"/api/jobs/{job.id}", {"scheduled_at": DAY2.isoformat()},
             409, "two_open_visits")


def test_refused_r4_crew_on_site(env):
    now = _now()
    job = _job(env, scheduled_at=now)
    _visit(env, job, now, arrived=now)
    _refused(env, "PATCH", f"/api/jobs/{job.id}", {"scheduled_at": DAY2.isoformat()},
             409, "crew_on_site")
    _refused(env, "PATCH", f"/api/v1/jobs/{job.id}", {"scheduled_at": DAY2.isoformat()},
             409, "crew_on_site")


def test_refused_needs_answer_rebook_closed_day(env):
    job = _job(env, stage="completed", completed_at=DAY1)
    _visit(env, job, DAY1, status="completed", arrived=DAY1)
    body = _refused(env, "POST", f"/api/jobs/{job.id}/uncomplete",
                    {"reason": "closed the wrong job",
                     "scheduled_at": (DAY1 + timedelta(hours=1)).isoformat()},
                    409, "needs_answer", question="rebook_closed_day")
    assert body["tech_ids"] == [T1]


def _status_only_job(env):
    job = _job(env)
    v = _visit(env, job, DAY1, status="arrived")  # an old row: status, no time
    return job, v


def test_refused_needs_answer_status_only_arrival_on_a_job_cancel(env):
    job, v = _status_only_job(env)
    body = _refused(env, "PATCH", f"/api/jobs/{job.id}", {"lifecycle_stage": "cancelled"},
                    409, "needs_answer", question="status_only_arrival")
    assert body["visit_ids"] == [str(v.id)]
    _refused(env, "PATCH", f"/api/v1/jobs/{job.id}", {"status": "cancelled"},
             409, "needs_answer", question="status_only_arrival")


def test_refused_needs_answer_status_only_arrival_on_an_appointment_cancel(env):
    _, v = _status_only_job(env)
    _refused(env, "POST", f"/api/appointments/{v.id}/cancel", {},
             409, "needs_answer", question="status_only_arrival")
    _refused(env, "PATCH", f"/api/appointments/{v.id}", {"status": "cancelled"},
             409, "needs_answer", question="status_only_arrival")


def test_refused_needs_answer_status_only_arrival_on_a_reactivate(env):
    job = _job(env, stage="cancelled")
    _visit(env, job, DAY1, status="arrived")
    _refused(env, "POST", f"/api/jobs/{job.id}/reactivate",
             {"reason": "customer called back"}, 409, "needs_answer",
             question="status_only_arrival")


def test_refused_appointment_delete_and_job_change_on_an_arrived_visit(env):
    now = _now()
    job = _job(env, scheduled_at=now)
    v = _visit(env, job, now, arrived=now)  # a tap: status still "scheduled"
    other = _job(env, scheduled_at=DAY3)
    _refused(env, "DELETE", f"/api/appointments/{v.id}", None, 409, "visit_arrived")
    _refused(env, "PATCH", f"/api/appointments/{v.id}", {"job_id": str(other.id)},
             409, "visit_arrived")
    _refused(env, "PATCH", f"/api/appointments/{v.id}", {"job_id": None},
             409, "visit_arrived")


def test_refused_public_patch_stage_rules(env):
    live = _job(env)
    _visit(env, live, DAY1)
    # The public API nests the shared refusal under `detail` (#864).
    body = _refused(env, "PATCH", f"/api/v1/jobs/{live.id}", {"status": "completed"}, 409)
    assert body["detail"]["use"] == "closeout", body
    done = _job(env, stage="completed", completed_at=DAY1)
    body = _refused(env, "PATCH", f"/api/v1/jobs/{done.id}", {"status": "scheduled"}, 409)
    assert body["detail"]["use"] == "reopen", body


# ── arrival is always recorded ────────────────────────────────────────


def test_patch_status_arrived_stamps_the_time(env):
    job = _job(env)
    v = _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/appointments/{v.id}", json={"status": "arrived"})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    row = env.db.get(Appointment, v.id)
    assert row.status == "arrived" and row.arrived_at is not None
    _assert_invariant_i(env.db, job.id)
    (audit,) = _new_audit(env.db, before, v.id)
    assert audit.details["arrived_at"]["old"] is None
    assert audit.details["arrived_at"]["source"] == "manual"


def test_office_arrived_stamps_now_when_unstamped(env):
    job = _job(env)
    v = _visit(env, job, DAY1)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/appointments/{v.id}/arrived", json={})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(Appointment, v.id).arrived_at is not None
    (audit,) = _new_audit(env.db, before, v.id)
    assert audit.details["source"] == "manual" and audit.details["arrived_at"]


def test_office_arrived_keeps_the_techs_stamp(env):
    tapped = _now() - timedelta(hours=1)
    job = _job(env, scheduled_at=tapped)
    v = _visit(env, job, tapped, arrived=tapped)
    before = _audit_ids(env.db)
    r = env.client.post(f"/api/appointments/{v.id}/arrived", json={})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    row = env.db.get(Appointment, v.id)
    assert _utc(row.arrived_at) == tapped and row.status == "arrived"
    _assert_invariant_i(env.db, job.id)
    (audit,) = _new_audit(env.db, before, v.id)
    assert "kept_arrived_at" in audit.details and "arrived_at" not in audit.details


def test_refused_office_arrived_with_a_time_on_a_stamped_visit(env):
    tapped = _now() - timedelta(hours=1)
    job = _job(env, scheduled_at=tapped)
    v = _visit(env, job, tapped, arrived=tapped)
    _refused(env, "POST", f"/api/appointments/{v.id}/arrived",
             {"arrived_at": (tapped + timedelta(minutes=5)).isoformat()}, 409, "arrival_recorded")


@pytest.mark.parametrize("status", ["scheduled", "arrived"])
def test_refused_patch_clearing_a_recorded_arrival(env, status):
    tapped = _now() - timedelta(hours=1)
    job = _job(env, scheduled_at=tapped)
    v = _visit(env, job, tapped, status=status, arrived=tapped)
    _refused(env, "PATCH", f"/api/appointments/{v.id}", {"arrived_at": None}, 422)
    _refused(env, "PATCH", f"/api/appointments/{v.id}",
             {"arrived_at": None, "status": "arrived"}, 422)


def test_resending_arrived_to_a_status_only_row_stamps_nothing(env):
    job, v = _status_only_job(env)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/appointments/{v.id}", json={"status": "arrived", "notes": "x"})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    row = env.db.get(Appointment, v.id)
    assert row.arrived_at is None and row.status == "arrived"
    _assert_invariant_i(env.db, job.id)
    (audit,) = _new_audit(env.db, before, v.id)
    assert "arrived_at" not in audit.details


def test_a_time_correction_audits_the_old_value(env):
    tapped = _now() - timedelta(hours=2)
    job = _job(env, scheduled_at=tapped)
    v = _visit(env, job, tapped, arrived=tapped)
    fixed = tapped - timedelta(minutes=20)
    before = _audit_ids(env.db)
    r = env.client.patch(f"/api/appointments/{v.id}", json={"arrived_at": fixed.isoformat()})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert _utc(env.db.get(Appointment, v.id).arrived_at) == fixed
    _assert_invariant_i(env.db, job.id)
    (audit,) = _new_audit(env.db, before, v.id)
    change = audit.details["arrived_at"]
    # SQLite hands the stored value back naive, so "old" carries no offset.
    assert _utc(datetime.fromisoformat(change["old"])) == tapped
    assert _utc(datetime.fromisoformat(change["new"])) == fixed
    assert change["source"] == "manual"
