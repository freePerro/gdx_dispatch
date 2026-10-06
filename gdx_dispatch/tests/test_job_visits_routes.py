"""The visits API and the Partial Jobs queue, over HTTP (multi-day jobs plan
§5.3a). Same harness as ``test_visit_writers.py``: a real ORM-built SQLite
database, invariant I checked from the rows, refusals checked against a
snapshot of every table they could touch."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text

from gdx_dispatch.models.tenant_models import JobAssignment
from gdx_dispatch.tests import test_visit_writers as _writers
from gdx_dispatch.tests.test_visit_writers import (
    DAY1,
    DAY2,
    DAY3,
    DAY4,
    OFFICE_USER,
    T1,
    T2,
    T3,
    TENANT,
    _actions,
    _assert_invariant_i,
    _audit_ids,
    _job,
    _layout,
    _new_audit,
    _refused,
    _utc,
    _visit,
)

env = _writers.env  # the writers' fixture, reused under its own name


@pytest.fixture()
def client(env):
    """The writers' app plus this PR's routes, with the dispatch module granted."""
    from gdx_dispatch.routers import dispatch_scheduling, job_visits

    app = env.client.app
    app.include_router(job_visits.router)
    app.include_router(dispatch_scheduling.router)
    env.db.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'dispatch', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-dispatch-{TENANT}", "tid": TENANT},
    )
    env.db.commit()
    return env


def _day(dt: datetime) -> str:
    # No settings row: the shop zone is America/New_York, and every DAY* is 10:00 there.
    return (dt - timedelta(hours=5)).date().isoformat()


def _slot(dt: datetime, minutes: int = 480) -> dict:
    return {"day": _day(dt), "start_time": "10:00", "duration_minutes": minutes}


def _crew(db, job_id) -> list[str]:
    db.expire_all()
    return [a.tech_id for a in db.execute(
        select(JobAssignment).where(JobAssignment.job_id == str(job_id), JobAssignment.deleted_at.is_(None))
    ).scalars().all()]


# ── GET ──────────────────────────────────────────────────────────────


def test_list_numbers_the_work_days_and_skips_a_cancelled_one(client):
    job = _job(client)
    _visit(client, job, DAY1, status="completed")
    _visit(client, job, DAY2, status="cancelled")  # never arrived: not a day of work
    _visit(client, job, DAY3)
    r = client.client.get(f"/api/jobs/{job.id}/visits")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["day_count"] == 2
    assert [(v["state"], v["day_index"]) for v in body["items"]] == [
        ("closed", 1), ("cancelled", None), ("open", 2),
    ]


def test_a_technician_can_read_the_list(client):
    job = _job(client)
    _visit(client, job, DAY1)
    client.be_tech()
    assert client.client.get(f"/api/jobs/{job.id}/visits").status_code == 200


# ── POST: Add day(s) ─────────────────────────────────────────────────


def test_add_days_books_each_and_audits_each(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    before = _audit_ids(client.db)
    r = client.client.post(f"/api/jobs/{job.id}/visits", json={
        "days": [_day(DAY2), _day(DAY3)], "start_time": "10:00", "duration_minutes": 480,
    })
    assert r.status_code == 201, r.text
    assert _layout(client.db, job.id) == [(T1, DAY1, "scheduled"), (T1, DAY2, "scheduled"), (T1, DAY3, "scheduled")]
    rows = [a for a in _new_audit(client.db, before, job.id) if a.action == "visit_added"]
    assert len(rows) == 2 and {a.details["reason"] for a in rows} == {"visits_booked"}
    assert all(a.user_id for a in rows)
    assert r.json()["day_count"] == 3
    _assert_invariant_i(client.db, job.id)


def test_adding_a_day_before_n_moves_the_job_date(client):
    job = _job(client, scheduled_at=DAY2, crew=(T1,))
    _visit(client, job, DAY2)
    before = _audit_ids(client.db)
    r = client.client.post(f"/api/jobs/{job.id}/visits", json={"days": [_day(DAY1)], "start_time": "10:00"})
    assert r.status_code == 201, r.text
    assert _utc(_assert_invariant_i(client.db, job.id).scheduled_at) == DAY1
    assert "job_schedule_recomputed" in _actions(client.db, before, job.id)


def test_add_for_named_techs_does_not_touch_the_crew(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    r = client.client.post(f"/api/jobs/{job.id}/visits", json={
        "days": [_day(DAY2)], "start_time": "10:00", "tech_ids": [T2],
    })
    assert r.status_code == 201, r.text
    assert _layout(client.db, job.id) == [(T1, DAY1, "scheduled"), (T2, DAY2, "scheduled")]
    assert _crew(client.db, job.id) == [T1]


def test_add_a_range_skipping_the_weekend(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    fri = DAY1 + timedelta(days=4)
    r = client.client.post(f"/api/jobs/{job.id}/visits", json={
        "range": {"from": _day(fri), "to": _day(fri + timedelta(days=3)), "skip_weekends": True},
        "start_time": "10:00",
    })
    assert r.status_code == 201, r.text
    assert [s for _t, s, _st in _layout(client.db, job.id)] == [DAY1, fri, fri + timedelta(days=3)]


def test_add_refusals_write_nothing(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    yesterday = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    _refused(client, "POST", f"/api/jobs/{job.id}/visits",
             {"days": [yesterday], "start_time": "10:00"}, 409, "past_day")
    _refused(client, "POST", f"/api/jobs/{job.id}/visits",
             {"days": [_day(DAY2), _day(DAY1)], "start_time": "10:00"}, 409, "double_booked",
             tech_ids=[T1], day=_day(DAY1))
    _refused(client, "POST", f"/api/jobs/{job.id}/visits",
             {"days": [_day(DAY2)], "start_time": "10:00", "tech_ids": ["no-such-tech"]}, 422)
    _refused(client, "POST", f"/api/jobs/{job.id}/visits",
             {"days": [_day(DAY2)], "range": {"from": _day(DAY2), "to": _day(DAY3)}, "start_time": "10:00"}, 422)


def test_add_on_a_finished_job_is_refused(client):
    job = _job(client, stage="completed", crew=(T1,))
    _refused(client, "POST", f"/api/jobs/{job.id}/visits",
             {"days": [_day(DAY2)], "start_time": "10:00"}, 409, "job_finished")


def test_a_technician_cannot_book_move_or_remove(client):
    job = _job(client, crew=(T1,))
    v1 = _visit(client, job, DAY1)
    _visit(client, job, DAY2)
    client.be_tech()
    _refused(client, "POST", f"/api/jobs/{job.id}/visits", {"days": [_day(DAY3)], "start_time": "10:00"}, 403)
    _refused(client, "PATCH", f"/api/jobs/{job.id}/visits/{v1.id}",
             _slot(DAY3), 403)
    _refused(client, "DELETE", f"/api/jobs/{job.id}/visits/{v1.id}", None, 403)


# ── PATCH / DELETE ───────────────────────────────────────────────────


def test_move_an_open_day_to_another_tech_and_day(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    v2 = _visit(client, job, DAY2)
    before = _audit_ids(client.db)
    r = client.client.patch(f"/api/jobs/{job.id}/visits/{v2.id}", json={**_slot(DAY3, 300), "tech_id": T2})
    assert r.status_code == 200, r.text
    assert _layout(client.db, job.id) == [(T1, DAY1, "scheduled"), (T2, DAY3, "scheduled")]
    assert _actions(client.db, before, job.id) == ["visit_moved", "visit_reassigned"]
    moved = next(v for v in r.json()["items"] if v["id"] == str(v2.id))
    # The day and time are read on the shop's clock, the length from the request.
    assert (moved["start_at"], moved["end_at"]) == (DAY3.isoformat(), (DAY3 + timedelta(hours=5)).isoformat())
    _assert_invariant_i(client.db, job.id)


def test_moving_the_first_day_later_moves_the_job_date(client):
    job = _job(client, crew=(T1,))
    v1 = _visit(client, job, DAY1)
    _visit(client, job, DAY3)
    r = client.client.patch(f"/api/jobs/{job.id}/visits/{v1.id}", json=_slot(DAY2))
    assert r.status_code == 200, r.text
    assert _utc(_assert_invariant_i(client.db, job.id).scheduled_at) == DAY2


def test_move_refusals_write_nothing(client):
    job = _job(client, crew=(T1,))
    v1 = _visit(client, job, DAY1, status="arrived", arrived=DAY1)
    v2 = _visit(client, job, DAY2)
    other = _job(client, crew=(T1,))
    foreign = _visit(client, other, DAY1)
    body = _slot(DAY3)
    _refused(client, "PATCH", f"/api/jobs/{job.id}/visits/{v1.id}", body, 409, "visit_not_open")
    _refused(client, "PATCH", f"/api/jobs/{job.id}/visits/{v2.id}",
             _slot(DAY1), 409, "double_booked")
    _refused(client, "PATCH", f"/api/jobs/{job.id}/visits/{v2.id}", _slot(DAY3, 0), 422)
    _refused(client, "PATCH", f"/api/jobs/{job.id}/visits/{foreign.id}", body, 404, "visit_not_found")


def test_remove_retires_a_day_and_keeps_the_row(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    v2 = _visit(client, job, DAY2)
    before = _audit_ids(client.db)
    r = client.client.delete(f"/api/jobs/{job.id}/visits/{v2.id}")
    assert r.status_code == 200, r.text
    assert _layout(client.db, job.id) == [(T1, DAY1, "scheduled")]
    client.db.expire_all()
    client.db.refresh(v2)
    assert v2.deleted_at is not None  # soft delete
    rows = _new_audit(client.db, before, job.id)
    assert [(a.action, a.details["reason"]) for a in rows] == [("visit_retired", "visit_removed")]


def test_remove_refuses_the_last_booked_day(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1, status="completed")
    v2 = _visit(client, job, DAY2)
    _refused(client, "DELETE", f"/api/jobs/{job.id}/visits/{v2.id}", None, 409, "last_open_visit")


# ── Partial Jobs and late-open ───────────────────────────────────────


def _partial_ids(client) -> list[str]:
    r = client.client.get("/api/dispatch/partial-jobs")
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


def _late_ids(client) -> list[str]:
    r = client.client.get("/api/dispatch/late-open")
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


def test_partial_membership_and_late_open_leave_each_other_out(client):
    past = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=3)
    partial = _job(client, scheduled_at=past, crew=(T1,))
    _visit(client, partial, past, status="completed")
    worked_after_arriving = _job(client, scheduled_at=past, crew=(T1,))
    _visit(client, worked_after_arriving, past, status="cancelled", arrived=past)
    late = _job(client, scheduled_at=past, crew=(T1,))
    _visit(client, late, past)  # still OPEN: a missed day, not a stopped one
    booked = _job(client, scheduled_at=DAY2, crew=(T1,))
    _visit(client, booked, past, status="completed")
    _visit(client, booked, DAY2)  # day 2 is booked: on the board, not partial
    finished = _job(client, scheduled_at=past, stage="completed", crew=(T1,))
    _visit(client, finished, past, status="completed")
    never_worked = _job(client, scheduled_at=past, crew=(T1,))
    _visit(client, never_worked, past, status="cancelled")
    legacy = _job(client, scheduled_at=past, crew=(T1,))  # no visit at all

    partial_ids = _partial_ids(client)
    assert sorted(partial_ids) == sorted([str(partial.id), str(worked_after_arriving.id)])
    late_ids = _late_ids(client)
    assert sorted(late_ids) == sorted([str(late.id), str(never_worked.id), str(legacy.id)])
    assert not set(partial_ids) & set(late_ids)


def test_partial_row_names_the_last_day_and_who_worked_it(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1, status="completed", tech=T2)
    _visit(client, job, DAY2, status="completed", tech=T1)
    [row] = client.client.get("/api/dispatch/partial-jobs").json()["items"]
    assert row["last_worked_day"] == _day(DAY2)
    # That day's techs only: T2 worked an earlier day.
    assert [w["tech_id"] for w in row["worked_by"]] == [T1]
    # Every closed (day, tech), which the board's E5 guard reads.
    assert row["closed_days"] == {_day(DAY1): [T2], _day(DAY2): [T1]}


def test_board_queue_rows_carry_the_hours_the_board_sums(client):
    """A queue row is a board card too: a parked partial job's lane total and
    the Scheduled lane's duration prompt read these, as GET /api/jobs gives
    them (the scheduler's own hours; none is None, not 0)."""
    partial = _job(client, crew=(T1,), scheduled_duration_hours=3)
    _visit(client, partial, DAY1, status="completed", tech=T1)
    past = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=3)
    late = _job(client, scheduled_at=past, assigned_to=None)  # no visit, no tech: late and in the Scheduled lane
    no_tech = _job(client, scheduled_at=DAY3, assigned_to=None, scheduled_duration_hours=2.5)
    [row] = client.client.get("/api/dispatch/partial-jobs").json()["items"]
    assert (row["scheduled_duration_hours"], row["effective_duration_hours"]) == (3.0, 3.0)
    late_rows = {i["id"]: i for i in client.client.get("/api/dispatch/late-open").json()["items"]}
    assert (late_rows[str(late.id)]["scheduled_duration_hours"], late_rows[str(late.id)]["effective_duration_hours"]) == (None, None)
    r = client.client.get("/api/dispatch/scheduled-unassigned")
    assert r.status_code == 200, r.text
    # Raw SQL: SQLite hands the id back as 32 hex, Postgres dashed.
    lane = {UUID(i["id"]).hex: i for i in r.json()["items"]}
    assert (lane[no_tech.id.hex]["scheduled_duration_hours"], lane[no_tech.id.hex]["effective_duration_hours"]) == (2.5, 2.5)
    assert lane[late.id.hex]["effective_duration_hours"] is None


def test_a_technician_cannot_read_the_partial_queue(client):
    client.be_tech()
    assert client.client.get("/api/dispatch/partial-jobs").status_code == 403


def test_a_board_drop_onto_a_tech_outside_the_crew_books_them_and_clears_partial(client):
    """The board's drop is PATCH /api/jobs/{id} with the tech and the date
    (DispatchView.vue assignJob). With no Current visit the planner books it
    by E4 and the crew becomes that tech, so the card lands in their column."""
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1, status="completed", tech=T1)
    assert _partial_ids(client) == [str(job.id)]
    before = _audit_ids(client.db)
    r = client.client.patch(f"/api/jobs/{job.id}", json={
        "assigned_tech_id": T2, "assigned_to": T2, "scheduled_at": DAY2.isoformat(),
    })
    assert r.status_code == 200, r.text
    assert _layout(client.db, job.id) == [(T1, DAY1, "completed"), (T2, DAY2, "scheduled")]
    assert _crew(client.db, job.id) == [T2]
    assert "visit_added" in _actions(client.db, before, job.id)
    _assert_invariant_i(client.db, job.id)
    assert _partial_ids(client) == []


def test_a_board_drop_onto_the_tech_who_closed_that_day_books_nothing(client):
    """E5: the tech already holds a Live visit on the day dropped on. The 200
    books nothing and the job stays partial; the board warns (vitest)."""
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1, status="completed", tech=T1)
    r = client.client.patch(f"/api/jobs/{job.id}", json={
        "assigned_tech_id": T1, "assigned_to": T1, "scheduled_at": (DAY1 - timedelta(hours=9)).isoformat(),
    })
    assert r.status_code == 200, r.text
    assert _layout(client.db, job.id) == [(T1, DAY1, "completed")]
    assert _partial_ids(client) == [str(job.id)]
    # ...yet the date is written (a time fix on a closed day is allowed), which
    # is why the board refuses this drop itself (closed_days).
    client.db.expire_all()
    [row] = client.client.get("/api/dispatch/partial-jobs").json()["items"]
    assert datetime.fromisoformat(row["scheduled_at"]) == DAY1 - timedelta(hours=9)


def test_audit_rows_carry_the_office_user(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    before = _audit_ids(client.db)
    client.client.post(f"/api/jobs/{job.id}/visits", json={"days": [_day(DAY2)], "start_time": "10:00"})
    rows = _new_audit(client.db, before, job.id)
    assert rows and {str(a.user_id) for a in rows} == {OFFICE_USER}


# ── The board's visit cards (PR 2b) ──────────────────────────────────


def _board(client, **params):
    r = client.client.get("/api/dispatch/visits", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _week(client) -> dict:
    return _board(client, date_from=_day(DAY1), date_to=_day(DAY4))


def test_move_without_a_length_keeps_a_visit_over_a_day(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    v2 = _visit(client, job, DAY2, hours=30)
    r = client.client.patch(f"/api/jobs/{job.id}/visits/{v2.id}", json={"day": _day(DAY3), "start_time": "10:00"})
    assert r.status_code == 200, r.text
    moved = next(v for v in r.json()["items"] if v["id"] == str(v2.id))
    assert (moved["start_at"], moved["end_at"]) == (DAY3.isoformat(), (DAY3 + timedelta(hours=30)).isoformat())
    # A typed length is still bounded.
    _refused(client, "PATCH", f"/api/jobs/{job.id}/visits/{v2.id}", _slot(DAY4, 30 * 60), 422)


def test_a_multi_day_job_is_one_card_per_live_day(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1, status="completed")
    _visit(client, job, DAY2, status="cancelled")  # never arrived: not drawn, not counted
    _visit(client, job, DAY3, tech=T2)
    body = _week(client)
    assert body["job_ids"] == [str(job.id)]
    assert [(i["visit_day"], i["visit_tech_id"], i["visit_state"], i["day_index"], i["day_count"])
            for i in body["items"]] == [
        (_day(DAY1), T1, "closed", 1, 2),
        (_day(DAY3), T2, "open", 2, 2),
    ]
    card = body["items"][1]
    assert card["id"] == str(job.id) and card["job_has_crew"] is True
    assert card["visit_start"] == DAY3.isoformat()
    assert card["visit_end"] == (DAY3 + timedelta(hours=8)).isoformat()
    # The window holds only what starts in it; day k of n still counts every day.
    [only] = _board(client, date=_day(DAY3))["items"]
    assert (only["day_index"], only["day_count"]) == (2, 2)


def test_a_one_day_job_its_job_row_draws_is_left_out(client):
    crewed = _job(client, assigned_to=T1, crew=(T1, T2))
    _visit(client, crewed, DAY1, tech=T1)
    _visit(client, crewed, DAY1, tech=T2, hours=4)  # length is not compared
    crewless = _job(client, assigned_to=None)
    _visit(client, crewless, DAY1, tech=None)
    legacy = _job(client, assigned_to=T1)  # no JobAssignment: the crew is assigned_to
    _visit(client, legacy, DAY1, tech=T1)
    assert _week(client) == {"items": [], "job_ids": [], "timezone": _week(client)["timezone"]}


def test_a_one_day_job_the_visits_card_changed_is_drawn_by_visit(client):
    removed = _job(client, crew=(T1, T2))
    _visit(client, removed, DAY1, tech=T1)  # T2's visit was removed
    moved = _job(client, crew=(T1,))
    _visit(client, moved, DAY1 + timedelta(hours=2), tech=T1)  # moved to noon
    outsider = _job(client, crew=(T1,))
    _visit(client, outsider, DAY1, tech=T3)
    open_slot = _job(client, crew=(T1,))
    _visit(client, open_slot, DAY1, tech=T1)
    _visit(client, open_slot, DAY1, tech=None)  # a crewed job's day with no tech
    body = _week(client)
    assert sorted(body["job_ids"]) == sorted(str(j.id) for j in (removed, moved, outsider, open_slot))
    slot = next(i for i in body["items"] if i["id"] == str(open_slot.id) and i["visit_tech_id"] is None)
    assert slot["job_has_crew"] is True


def test_a_deleted_job_is_out(client):
    job = _job(client, crew=(T1,), deleted_at=DAY1 - timedelta(days=1))
    _visit(client, job, DAY1)
    _visit(client, job, DAY2)
    assert _week(client)["items"] == []


def test_the_window_is_cut_on_the_shop_day(client):
    job = _job(client, crew=(T1,))
    _visit(client, job, DAY1)
    evening = DAY2 - timedelta(hours=13)  # 02:00 UTC on day 2 is 21:00 on day 1, shop time
    _visit(client, job, evening, hours=1)
    assert [i["visit_start"] for i in _board(client, date=_day(DAY1))["items"]] == [
        DAY1.isoformat(), evening.isoformat(),
    ]
    assert _board(client, date=_day(DAY2))["items"] == []


def test_board_visits_is_gated_and_bounded(client):
    for params in (
        {"date_from": _day(DAY2), "date_to": _day(DAY1)},
        {"date_from": _day(DAY1), "date_to": _day(DAY1 + timedelta(days=366))},
        {"date": "not-a-day"},
        {},
    ):
        assert client.client.get("/api/dispatch/visits", params=params).status_code == 422, params
    assert client.client.get(
        "/api/dispatch/visits", params={"date_from": _day(DAY1), "date_to": _day(DAY1 + timedelta(days=365))},
    ).status_code == 200
    client.be_tech()
    assert client.client.get("/api/dispatch/visits", params={"date": _day(DAY1)}).status_code == 403
