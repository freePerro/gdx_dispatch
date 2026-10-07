"""POST /api/jobs/{id}/day-close — "No" on "Is this job finished?" (multi-day
jobs plan §5.4a): money, replay, refusals and state.

The route functions are called directly against an ORM-built SQLite schema,
as test_closeout_labor_trail does. Payroll is read through payroll's OWN query
(``_fetch_tech_hours``) and billing through ``day_row_entries``, the rows a
labor line prices, never a paraphrase of either. The Postgres-only concurrency
tests skip without ``GDX_PROOF_PG_URL``.

Every date here is relative to the real shop "today": the closeout's day
refusals and the day log read server time, so a fixed calendar would rot.
Timestamps sit at 00:05 shop time, so even "today" is never in the future.

The other ``test_day_close_*`` files import this harness.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy import text as _sa_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobAssignment,
    Technician,
    TimeEntry,
    User,
)
from gdx_dispatch.routers import jobs as jobs_router
from gdx_dispatch.routers.jobs import day_close_job, get_job_day_log
from gdx_dispatch.services.visit_sync import shop_day, shop_instant

TENANT = "tenant-dc"
TZ = "America/New_York"  # shop_tz's fallback with no AppSettings row
LEAD = str(uuid4())
HELPER = str(uuid4())
THIRD = str(uuid4())
OFFICE = str(uuid4())
STOP_NOTE = "Timer stopped on mobile"


def today():
    return shop_day(datetime.now(UTC), TZ)


def at(offset_days: int, minute: int = 5):
    """00:MM shop time on today + offset_days."""
    return shop_instant(today() + timedelta(days=offset_days), time(0, minute), TZ)


def aware(v):
    return v.replace(tzinfo=UTC) if v is not None and v.tzinfo is None else v


@pytest.fixture
def db():
    pg_url = os.environ.get("GDX_PROOF_PG_URL")
    if pg_url:
        engine = create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(_sa_text("DROP SCHEMA public CASCADE"))
            conn.execute(_sa_text("CREATE SCHEMA public"))
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    # The role column is read for permissions on Postgres (SQLite's str-vs-Uuid
    # bind fails that lookup and falls back to the token's claim).
    for uid, name, role in ((LEAD, "Lead Larry", "technician"), (HELPER, "Helper Hank", "technician"),
                            (THIRD, "Third Tom", "technician"), (OFFICE, "Office Olga", "admin")):
        session.add(User(id=UUID(uid), full_name=name, company_id=TENANT, username=name, role=role))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    flags = {
        "lock_schedule_on_start": False, "post_arrival_event": False,
        "sms_arrival_notify": False, "require_parts_on_complete": False,
        "require_hours_on_complete": False, "require_signature_on_complete": False,
        "require_invoice_on_complete": False,
    }
    monkeypatch.setattr(jobs_router, "_load_workflow_flags", lambda _tid: dict(flags))
    return flags


def request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def tech_user(uid: str = LEAD) -> dict:
    return {"user_id": uid, "sub": uid, "tenant_id": TENANT, "role": "technician"}


def office_user() -> dict:
    return {"user_id": OFFICE, "sub": OFFICE, "tenant_id": TENANT, "role": "admin"}


def technician(db, uid: str, rate: float = 40.0) -> Technician:
    t = Technician(
        id=str(uuid4()), company_id=TENANT, user_id=uid, name=f"Tech {uid[:4]}",
        active=True, hourly_rate=Decimal(str(rate)), created_at=datetime.now(UTC),
    )
    db.add(t)
    db.commit()
    return t


def make_job(db, *, assigned_to: str = LEAD, stage: str = "in_progress",
             job_type: str = "Service Call") -> Job:
    customer = Customer(id=uuid4(), name="Day Close Customer", company_id=TENANT)
    db.add(customer)
    db.flush()
    job = Job(
        id=uuid4(), customer_id=customer.id, title="Door repair", company_id=TENANT,
        lifecycle_stage=stage, status="In Progress", dispatch_status="on_site",
        billing_status="unbilled", assigned_to=assigned_to, job_type=job_type,
        scheduled_at=at(0),
    )
    db.add(job)
    db.commit()
    return job


def visit(db, job, start, *, tech: Technician | None = None, status: str = "scheduled",
          arrived: bool = False, en_route: bool = False) -> Appointment:
    a = Appointment(
        id=uuid4(), company_id=TENANT, job_id=job.id,
        tech_id=tech.id if tech is not None else None,
        title=job.title, start_at=start, end_at=start + timedelta(hours=8),
        status="arrived" if arrived else status,
        arrived_at=start if arrived else None,
        en_route_at=start if (en_route or arrived) else None,
    )
    db.add(a)
    db.commit()
    return a


def crew(db, job, tech: Technician) -> None:
    """A crew row: how a helper with no visit of their own reaches the job.

    job_access joins ``CAST(jobs.id AS TEXT) = CAST(job_assignments.job_id AS
    TEXT)``; SQLite stores ``jobs.id`` as 32 dashless hex, so the crew row has
    to be spelled the same way there for the join to see it.
    """
    jid = job.id.hex if db.get_bind().dialect.name == "sqlite" else str(job.id)
    db.add(JobAssignment(id=str(uuid4()), job_id=jid, tech_id=tech.id, user_id=tech.user_id))
    db.commit()


def timer(db, job, uid: str, clock_in, *, tech: Technician | None = None,
          stopped: bool = False) -> TimeEntry:
    """What mobile_job_arrived / mobile_clock_in write; ``stopped`` is the
    mobile Stop (closed, 0 minutes, the marker note)."""
    t = TimeEntry(
        id=uuid4(), company_id=TENANT, job_id=job.id,
        tech_id=tech.id if tech is not None else uid, user_id=uid,
        clock_in=clock_in, entry_type="job", created_at=clock_in,
        clock_out=clock_in + timedelta(hours=1) if stopped else None,
        duration_minutes=0 if stopped else None,
        notes=f"{STOP_NOTE} (elapsed 60 min)" if stopped else None,
    )
    db.add(t)
    db.commit()
    return t


def body(day_offset: int = 0, *, visits=(), people=(), added=(), closed_at=None, note=None) -> dict:
    out = {
        "day": (today() + timedelta(days=day_offset)).isoformat(),
        "visits": [str(v.id) for v in visits],
        "people": [{"user_id": u, "hours": h} for u, h in people],
        "added": [{"hours": h} for h in added],
        "closed_at": (closed_at or datetime.now(UTC)).isoformat(),
    }
    if note is not None:
        out["note"] = note
    return out


def day_close(db, job, payload, user=None):
    resp = day_close_job(
        job_id=str(job.id), request=request(), body=payload,
        current_user=user or tech_user(), db=db,
    )
    return resp.status_code, json.loads(resp.body)


def day_log(db, job, user=None):
    resp = get_job_day_log(job_id=str(job.id), request=request(), current_user=user or tech_user(), db=db)
    return resp.status_code, json.loads(resp.body)


def entries(db, job) -> list[TimeEntry]:
    db.expire_all()
    return list(db.execute(
        select(TimeEntry).where(TimeEntry.job_id == job.id).order_by(TimeEntry.clock_in, TimeEntry.id)
    ).scalars().all())


def paid(db) -> dict[str, float]:
    from gdx_dispatch.routers.payroll import _fetch_tech_hours

    by_tech = _fetch_tech_hours(
        db, tenant_id=TENANT,
        start=(datetime.now(UTC) - timedelta(days=60)).date(),
        end=(datetime.now(UTC) + timedelta(days=60)).date(),
    )
    totals = {tech: sum(days.values()) for tech, days in by_tech.items()}
    return {tech: round(h, 2) for tech, h in totals.items() if h}


def billed_man_hours(db, job) -> float:
    """The raw man-hours of the job's day rows, billed or not: what the labor
    prices before it rounds (an autodraft may already hold them)."""
    from gdx_dispatch.core.closeout_billing import day_row_entries

    db.expire_all()
    rows = day_row_entries(db, [job.id]).get(str(job.id), [])
    return round(sum(minutes for _at, minutes in rows) / 60, 2)


def audits(db, job, action: str) -> list[AuditLog]:
    return list(db.execute(
        select(AuditLog).where(AuditLog.entity_id == str(job.id), AuditLog.action == action)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).scalars().all())


# --------------------------------------------------------------- money


def test_solo_tech_open_timer_becomes_the_day_row(db):
    t = technician(db, LEAD, rate=40)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    tm = timer(db, job, LEAD, at(0), tech=t)

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8)], note="Rails hung"))

    assert code == 200, out
    assert out["replay"] is False
    rows = entries(db, job)
    assert [r.id for r in rows] == [tm.id], "the timer is restated, not duplicated"
    row = rows[0]
    assert row.user_id == LEAD and row.duration_minutes == 480
    assert float(row.hourly_rate) == 40.0
    assert row.notes == "Rails hung"
    assert row.day_closed_at is not None
    db.refresh(v)
    assert v.status == "completed" and v.day_closed_at is not None
    assert paid(db) == {LEAD: 8.0}
    assert billed_man_hours(db, job) == 8.0
    assert out["day_rows"] == [{"id": str(tm.id), "person_name": "Lead Larry", "hours": 8.0}]


def test_stop_marked_timer_is_restated_even_three_days_later(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-3), tech=t, arrived=True)
    visit(db, job, at(1), tech=t)
    tm = timer(db, job, LEAD, at(-3), tech=t, stopped=True)

    code, out = day_close(db, job, body(-3, visits=[v], people=[(LEAD, 6)]))

    assert code == 200, out
    rows = entries(db, job)
    assert len(rows) == 1 and rows[0].id == tm.id
    assert rows[0].duration_minutes == 360 and rows[0].user_id == LEAD
    assert paid(db) == {LEAD: 6.0}


def test_same_day_return_is_one_row_and_other_timers_close_at_zero(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v1 = visit(db, job, at(0, 5), tech=t, arrived=True)
    v2 = visit(db, job, at(0, 30), tech=t, arrived=True)
    first = timer(db, job, LEAD, at(0, 5), tech=t, stopped=True)
    second = timer(db, job, LEAD, at(0, 30), tech=t)

    code, out = day_close(db, job, body(visits=[v1, v2], people=[(LEAD, 8)]))

    assert code == 200, out
    by_id = {r.id: r for r in entries(db, job)}
    assert len(by_id) == 2
    assert by_id[second.id].duration_minutes == 480, "the open timer is the You row"
    assert by_id[first.id].duration_minutes == 0
    assert by_id[first.id].day_closed_at is not None
    assert paid(db) == {LEAD: 8.0}
    assert billed_man_hours(db, job) == 8.0


@pytest.mark.parametrize("submitter", [HELPER, LEAD])
def test_two_people_one_unassigned_visit_mirrors_by_submitter(db, submitter):
    """Round-32 fixture A: T and U both tap unassigned V1, U also a same-day V2."""
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v1 = visit(db, job, at(0, 5), arrived=True)
    v2 = visit(db, job, at(0, 40), tech=th, arrived=True)
    lead_timer = timer(db, job, LEAD, at(0, 5), tech=tl)
    helper_timer = timer(db, job, HELPER, at(0, 10), tech=th)
    other = LEAD if submitter == HELPER else HELPER

    _code, log = day_log(db, job, tech_user(submitter))
    assert sorted(p["user_id"] for p in log["open_day"]["people"]) == sorted([LEAD, HELPER])
    assert log["open_day"]["people"][0]["user_id"] == submitter, "You first"
    assert log["open_day"]["people"][0]["mine"] is True

    code, out = day_close(db, job, body(visits=[v1, v2], people=[(LEAD, 8), (HELPER, 8)]),
                          tech_user(submitter))

    assert code == 200, out
    rows = entries(db, job)
    own = lead_timer if submitter == LEAD else helper_timer
    theirs = helper_timer if submitter == LEAD else lead_timer
    by_id = {r.id: r for r in rows}
    assert by_id[own.id].duration_minutes == 480 and by_id[own.id].user_id == submitter
    assert by_id[theirs.id].duration_minutes == 0 and by_id[theirs.id].day_closed_at is not None
    synthetic = [r for r in rows if r.id not in (own.id, theirs.id)]
    assert len(synthetic) == 1
    assert synthetic[0].user_id is None and synthetic[0].entry_type == "work"
    assert synthetic[0].tech_id == theirs.tech_id and synthetic[0].duration_minutes == 480
    assert aware(synthetic[0].clock_in) == aware(theirs.clock_in)
    assert paid(db) == {submitter: 8.0}, f"{other} is not paid by someone else's No"
    assert billed_man_hours(db, job) == 16.0


def test_office_completed_first_visit_then_retap_is_one_row(db):
    """Round-32 fixture B."""
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(0, 5), tech=t, status="completed", arrived=True)
    v2 = visit(db, job, at(0, 30), arrived=True)
    timer(db, job, LEAD, at(0, 30), tech=t)

    _c, log = day_log(db, job)
    assert [p["user_id"] for p in log["open_day"]["people"]] == [LEAD]
    code, out = day_close(db, job, body(visits=[v2], people=[(LEAD, 8)]))
    assert code == 200, out
    assert paid(db) == {LEAD: 8.0} and billed_man_hours(db, job) == 8.0


def test_a3_helper_with_no_visit_is_a_person(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    h = timer(db, job, HELPER, at(0, 20), tech=th)

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8), (HELPER, 6)]))

    assert code == 200, out
    rows = entries(db, job)
    assert next(r for r in rows if r.id == h.id).duration_minutes == 0
    nulls = [r for r in rows if r.user_id is None]
    assert [(r.duration_minutes, r.tech_id) for r in nulls] == [(360, th.id)]


def test_desk_no_pays_nobody_and_bills_both(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8), (HELPER, 8)]), office_user())

    assert code == 200, out
    rows = entries(db, job)
    assert len([r for r in rows if r.user_id is None and r.duration_minutes == 480]) == 2
    assert all(r.duration_minutes == 0 for r in rows if r.user_id is not None)
    assert paid(db) == {}
    assert billed_man_hours(db, job) == 16.0


def test_added_helper_row(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0, 15), tech=t, arrived=True)
    timer(db, job, LEAD, at(0, 15), tech=t)

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8)], added=[4]))

    assert code == 200, out
    added = [r for r in entries(db, job) if r.user_id is None]
    assert len(added) == 1
    row = added[0]
    assert row.duration_minutes == 240 and row.entry_type == "work"
    assert row.tech_id == LEAD and row.notes == "Added helper (not tapped in)"
    assert aware(row.clock_in) == at(0, 15), "anchored to the earliest listed visit"
    assert {"id": str(row.id), "person_name": "Added helper", "hours": 4.0} in out["day_rows"]
    assert paid(db) == {LEAD: 8.0} and billed_man_hours(db, job) == 12.0


def test_submitter_without_a_timer_has_no_you_row_and_added_pays_nobody(db):
    job = make_job(db)
    v = visit(db, job, at(0), arrived=True)

    code, out = day_close(db, job, body(visits=[v], added=[5]))

    assert code == 200, out
    assert paid(db) == {}
    assert billed_man_hours(db, job) == 5.0
    _c, log = day_log(db, job)
    assert all(not p["mine"] for p in log["open_day"]["people"])


def test_unchecked_person_is_untouched_and_later_closes_their_own_day(db):
    tl, th, tt = technician(db, LEAD), technician(db, HELPER), technician(db, THIRD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    h = timer(db, job, HELPER, at(0, 10), tech=th)
    third = timer(db, job, THIRD, at(0, 20), tech=tt)
    crew(db, job, th)

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8), (THIRD, 7)]))
    assert code == 200, out
    db.expire_all()
    h = db.get(TimeEntry, h.id)
    assert h.clock_out is None and h.day_closed_at is None, "unchecked: left exactly as it was"
    assert db.get(TimeEntry, third.id).day_closed_at is not None

    code, out = day_close(db, job, body(people=[(HELPER, 5)]), tech_user(HELPER))
    assert code == 200, out
    assert paid(db) == {LEAD: 8.0, HELPER: 5.0}
    assert billed_man_hours(db, job) == 20.0


def test_visits_only_body_closes_visits_and_writes_no_rows(db):
    job = make_job(db)
    v = visit(db, job, at(0))
    code, out = day_close(db, job, body(visits=[v]))
    assert code == 200, out
    assert entries(db, job) == []
    db.refresh(v)
    assert v.status == "completed" and v.day_closed_at is not None
    assert out["day_rows"] == []


# --------------------------------------------------------- replay


def test_lost_response_replay_is_a_noop_even_after_the_job_finished(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t)
    timer(db, job, LEAD, at(-1), tech=t)
    payload = body(-1, visits=[v], people=[(LEAD, 8)])

    assert day_close(db, job, payload)[0] == 200
    before = [(r.id, r.duration_minutes) for r in entries(db, job)]
    job = db.get(Job, job.id)
    job.lifecycle_stage = "completed"
    db.commit()

    code, out = day_close(db, job, payload)

    assert code == 200 and out["replay"] is True, out
    assert [(r.id, r.duration_minutes) for r in entries(db, job)] == before
    assert len(out["day_rows"]) == 1 and out["day_rows"][0]["hours"] == 8.0
    assert len(audits(db, job, "job_day_closed")) == 1


def test_visits_only_replay_matches_through_the_visit_marker(db):
    job = make_job(db)
    v = visit(db, job, at(0))
    payload = body(visits=[v])
    assert day_close(db, job, payload)[0] == 200
    code, out = day_close(db, job, payload)
    assert code == 200 and out["replay"] is True, out


def test_replay_with_a_closed_at_ahead_of_server_time_is_the_noop(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    ahead = datetime.now(UTC) + timedelta(minutes=7)
    payload = body(visits=[v], people=[(LEAD, 3)], closed_at=ahead)

    assert day_close(db, job, payload)[0] == 200
    db.refresh(v)
    assert aware(v.completed_at) <= datetime.now(UTC), "completed_at is clamped"
    assert aware(v.day_closed_at) == ahead, "the key is the raw value"
    code, out = day_close(db, job, payload)
    assert code == 200 and out["replay"] is True


def test_closed_at_in_another_zone_is_the_same_key(db):
    job = make_job(db)
    v = visit(db, job, at(0))
    instant = datetime.now(UTC).replace(microsecond=123456)
    first = body(visits=[v], closed_at=instant)
    assert day_close(db, job, first)[0] == 200
    second = dict(first, closed_at=instant.astimezone(
        __import__("zoneinfo").ZoneInfo("America/Chicago")).isoformat())
    code, out = day_close(db, job, second)
    assert code == 200 and out["replay"] is True


def test_queued_no_after_the_office_yes_is_job_finished_not_200(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job

    resp = closeout_job(
        payload=CloseoutPayload(parts=[], hours=6, no_parts_used=True), job_id=str(job.id),
        request=request(), current_user=office_user(), db=db,
    )
    assert resp.status_code == 201, resp.body

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8)]))
    assert code == 409 and out["code"] == "job_finished", out


# ------------------------------------------------------- refusals


def test_visit_not_open_cases(db):
    t = technician(db, LEAD)
    job = make_job(db)
    other_job = make_job(db)
    by_hand = visit(db, job, at(0, 5), tech=t, status="completed")
    deleted = visit(db, job, at(0, 6), tech=t)
    deleted.deleted_at = datetime.now(UTC)
    db.commit()
    foreign = visit(db, other_job, at(0, 7), tech=t)

    for v in (by_hand, deleted, foreign):
        code, out = day_close(db, job, body(visits=[v]))
        assert code == 409 and out["code"] == "visit_not_open", out
        assert "already_closed" not in out, "only a day-close's own marker says who"


def test_visit_closed_by_another_day_close_carries_already_closed(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    assert day_close(db, job, body(visits=[v], people=[(LEAD, 7)]))[0] == 200

    code, out = day_close(db, job, body(visits=[v]), office_user())

    assert code == 409 and out["code"] == "visit_not_open"
    assert out["already_closed"] == {"rows": [
        {"person_name": "Lead Larry", "hours": 7.0, "closed_by": "Lead Larry"},
    ]}


def test_day_moved_leaves_the_visit_and_timer_open(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t)
    tm = timer(db, job, LEAD, at(0), tech=t)
    payload = body(visits=[v], people=[(LEAD, 8)])
    v.start_at = at(1)  # the office moves it to tomorrow before the replay
    db.commit()

    code, out = day_close(db, job, payload)

    assert code == 409 and out["code"] == "day_moved"
    assert out["detail"] == "The visit moved; reopen the sheet."
    db.expire_all()
    assert db.get(Appointment, v.id).status == "scheduled"
    assert db.get(TimeEntry, tm.id).clock_out is None


def test_person_not_open_and_already_closed(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)
    crew(db, job, th)

    code, out = day_close(db, job, body(people=[(THIRD, 2)]))
    assert code == 409 and out["code"] == "person_not_open" and "already_closed" not in out

    assert day_close(db, job, body(visits=[v], people=[(LEAD, 8), (HELPER, 4)]))[0] == 200
    code, out = day_close(db, job, body(people=[(HELPER, 4)]), tech_user(HELPER))
    assert code == 409 and out["code"] == "person_not_open"
    assert {"person_name": "Helper Hank", "hours": 4.0, "closed_by": "Lead Larry"} in out["already_closed"]["rows"]


def test_check_order_replay_before_finished_before_visit(db):
    """The first failing check answers: job_finished outranks visit_not_open."""
    job = make_job(db, stage="completed")
    v = visit(db, job, at(0), status="completed")
    code, out = day_close(db, job, body(visits=[v]))
    assert out.get("code") == "job_finished", out


GOOD_DAY = None


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda b, v: b.update(visits=[str(v.id), str(v.id)]), id="duplicate-visit"),
    pytest.param(lambda b, v: b.update(people=[{"user_id": LEAD, "hours": 1}, {"user_id": LEAD, "hours": 2}]),
                 id="duplicate-user"),
    *[pytest.param(lambda b, v, h=h: b.update(people=[{"user_id": LEAD, "hours": h}]), id=f"people-{h}")
      for h in (0, 24.5, -4)],
    *[pytest.param(lambda b, v, h=h: b.update(added=[{"hours": h}]), id=f"added-{h}")
      for h in (0, 24.5, -4)],
    pytest.param(lambda b, v: b.update(added=[{"hours": 1}] * 11), id="eleven-added"),
    pytest.param(lambda b, v: b.update(visits=[], people=[], added=[]), id="empty"),
    pytest.param(lambda b, v: b.update(note="x" * 2001), id="long-note"),
    pytest.param(lambda b, v: b.pop("day"), id="day-missing"),
    pytest.param(lambda b, v: b.update(day="11/02/2026"), id="day-malformed"),
    pytest.param(lambda b, v: b.pop("closed_at"), id="closed_at-missing"),
    pytest.param(lambda b, v: b.update(closed_at="yesterday"), id="closed_at-malformed"),
    pytest.param(lambda b, v: b.update(closed_at="2026-10-06T10:00:00"), id="closed_at-naive"),
])
def test_shape_422s(db, mutate):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    payload = body(visits=[v], people=[(LEAD, 2)])
    mutate(payload, v)

    code, out = day_close(db, job, payload)

    assert code == 422, out
    assert isinstance(out["detail"], str)
    assert entries(db, job)[0].clock_out is None


def test_day_two_no_replayed_on_day_three_leaves_day_three_alone(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v2 = visit(db, job, at(-1), tech=t, arrived=True)
    v3 = visit(db, job, at(0), tech=t, arrived=True)
    d2 = timer(db, job, LEAD, at(-1), tech=t)
    d3 = timer(db, job, LEAD, at(0), tech=t)

    code, out = day_close(db, job, body(-1, visits=[v2], people=[(LEAD, 8)]))

    assert code == 200, out
    db.expire_all()
    assert db.get(TimeEntry, d2.id).duration_minutes == 480
    assert db.get(TimeEntry, d3.id).clock_out is None and db.get(TimeEntry, d3.id).day_closed_at is None
    assert db.get(Appointment, v3.id).status == "arrived"


def test_a_failure_in_the_people_step_leaves_the_visits_open(db, monkeypatch):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    from sqlalchemy.exc import OperationalError

    def boom(*_a, **_k):
        raise OperationalError("UPDATE", {}, Exception("disk full"))

    monkeypatch.setattr(jobs_router, "_close_labor_entry", boom)

    code, _out = day_close(db, job, body(visits=[v], people=[(LEAD, 8)]))

    assert code == 500
    db.expire_all()
    assert db.get(Appointment, v.id).status == "arrived"
    assert db.get(Appointment, v.id).day_closed_at is None
    assert audits(db, job, "visit_closed") == []


# ---------------------------------------------------------- state


@pytest.mark.parametrize(("remaining", "expected"), [
    (None, "assigned"), ("en_route", "en_route"), ("arrived", "on_site"),
])
def test_dispatch_status_rolls_up_from_the_remaining_visits_of_that_day(db, remaining, expected):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    closing = visit(db, job, at(0, 5), tech=tl, arrived=True)
    if remaining:
        visit(db, job, at(0, 30), tech=th, arrived=remaining == "arrived",
              en_route=remaining == "en_route")
    visit(db, job, at(1), tech=tl)
    timer(db, job, LEAD, at(0, 5), tech=tl)

    assert day_close(db, job, body(visits=[closing], people=[(LEAD, 8)]))[0] == 200
    db.expire_all()
    assert db.get(Job, job.id).dispatch_status == expected


def test_schedule_recomputes_to_the_next_visit(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    nxt = visit(db, job, at(2), tech=t)
    code, out = day_close(db, job, body(visits=[v]))
    assert code == 200
    db.expire_all()
    assert aware(db.get(Job, job.id).scheduled_at) == at(2)
    assert out["next_visit"] == {"id": str(nxt.id), "start_at": at(2).isoformat()}


def test_last_visit_closed_lands_the_job_in_partial_jobs(db):
    from gdx_dispatch.services.visit_sync import partial_clause

    job = make_job(db)
    v = visit(db, job, at(0), arrived=True)
    code, out = day_close(db, job, body(visits=[v]))
    assert code == 200 and out["next_visit"] is None
    db.expire_all()
    assert db.execute(select(Job.id).where(Job.id == job.id, partial_clause(Job.id))).first() is not None
    assert db.get(Job, job.id).lifecycle_stage == "in_progress", "never touches the stage"


def test_audit_names_every_person_with_hours_and_row(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)

    code, out = day_close(db, job, body(visits=[v], people=[(LEAD, 8), (HELPER, 3.5)], added=[2],
                                        note="Back tomorrow"))
    assert code == 200
    rows = audits(db, job, "job_day_closed")
    assert len(rows) == 1
    d = rows[0].details
    assert d["day"] == today().isoformat()
    assert d["visits"] == [str(v.id)]
    assert {(p["user_id"], p["hours"]) for p in d["people"]} == {(LEAD, 8.0), (HELPER, 3.5)}
    live = {r.id for r in entries(db, job) if (r.duration_minutes or 0) > 0}
    assert {UUID(p["day_row_id"]) for p in d["people"]} | {UUID(a["day_row_id"]) for a in d["added"]} == {
        UUID(str(i)) for i in live}
    assert d["note"] == "Back tomorrow" and d["actor"] == LEAD
    assert rows[0].user_id == LEAD
    assert len(audits(db, job, "visit_closed")) == 1
    assert len(audits(db, job, "day_row_created")) == 2


def test_null_rows_are_work_rows_no_job_reader_sees(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)
    assert day_close(db, job, body(people=[(LEAD, 8), (HELPER, 8)], added=[1]))[0] == 200
    assert {r.entry_type for r in entries(db, job) if r.user_id is None} == {"work"}


# ----------------------------------------------- Postgres concurrency

_PG = os.environ.get("GDX_PROOF_PG_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _PG,
    reason="the day-close concurrency tests need a real Postgres (SQLite ignores FOR UPDATE); "
    "set GDX_PROOF_PG_URL",
)


@_requires_pg
def test_pg_two_sessions_same_body_one_set_of_rows(db):
    import threading

    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    payload = body(visits=[v], people=[(LEAD, 8)])
    Session = sessionmaker(bind=db.get_bind())
    results = []

    def run():
        s = Session()
        try:
            results.append(day_close(s, job, payload))
        finally:
            s.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(c for c, _ in results) == [200, 200]
    assert sorted(o["replay"] for _, o in results) == [False, True]
    assert len([r for r in entries(db, job) if (r.duration_minutes or 0) > 0]) == 1


@_requires_pg
def test_pg_people_only_and_lead_no_naming_u_leave_one_row(db):
    import threading

    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)
    crew(db, job, th)
    Session = sessionmaker(bind=db.get_bind())
    results = []

    def run(payload, user):
        s = Session()
        try:
            results.append(day_close(s, job, payload, user))
        finally:
            s.close()

    threads = [
        threading.Thread(target=run, args=(body(people=[(HELPER, 4)]), tech_user(HELPER))),
        threading.Thread(target=run, args=(body(visits=[v], people=[(LEAD, 8), (HELPER, 4)]), tech_user(LEAD))),
    ]
    for t_ in threads:
        t_.start()
    for t_ in threads:
        t_.join()
    assert sorted(c for c, _ in results) == [200, 409]
    helper_rows = [
        r for r in entries(db, job)
        if (r.duration_minutes or 0) > 0 and (r.user_id == HELPER or r.tech_id == th.id)
    ]
    assert len(helper_rows) == 1
