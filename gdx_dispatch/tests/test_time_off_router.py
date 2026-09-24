"""routers/time_off.py — requests, rulings, direct entries, holiday pay.

Every assertion reads the real rows back (a mock proves which arguments were
passed, never which value comes back). The harness is the ORM-built one from
test_silent_success_sweep: create_all schema, login dicts with BOTH sub and
user_id (#701), tenant injected by middleware.

What is pinned, named for the failure it prevents:

* a tech's request lands pending with an audit row, and the office's approval
  writes exactly the workday entries, the entry ids on the request, and one
  audit row — in the same transaction;
* a tech cannot request for, cancel for, approve for, or see anyone else;
  a viewer cannot request at all;
* approving twice, or posting a holiday twice, pays nobody twice;
* revoke removes only the entries the approval created, never a worked shift
  on the same day;
* a tech cannot stretch an approved day through the shift-edit route.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.pay_periods import shop_day_of, shop_today
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry, TimeOffRequest, User
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.test_silent_success_sweep import TENANT, _build_client, _login, _teardown

TZ = "America/Chicago"
TECH = str(uuid4())
OTHER_TECH = str(uuid4())
OFFICE = str(uuid4())


def _client(role: str = "technician", user_id: str = TECH):
    from gdx_dispatch.routers import time_off as mod

    return _build_client(mod.router, user=_login(user_id, role), module_keys=("timeclock",))


def _as(client, user_id: str, role: str) -> None:
    client.app.dependency_overrides[get_current_user] = lambda: _login(user_id, role)


def _seed(client, *, workdays: int = 31, calendar: list | None = None, **settings):
    with client.SessionLocal() as db:
        db.add(AppSettings(
            company_name="Shop", timezone=TZ, default_workdays=workdays,
            holiday_calendar=calendar or [], **settings,
        ))
        db.add(User(id=UUID(TECH), company_id=TENANT, full_name="Amber Rosa", role="technician"))
        db.add(User(id=UUID(OTHER_TECH), company_id=TENANT, full_name="Michael T", role="technician"))
        db.add(User(id=UUID(OFFICE), company_id=TENANT, full_name="Office", role="dispatcher"))
        db.commit()


def _next(weekday: int, *, after: date | None = None) -> date:
    """The next date with this weekday (Mon=0) strictly after `after` (today)."""
    day = (after or shop_today(TZ)) + timedelta(days=1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


def _fri_to_mon() -> tuple[date, date]:
    fri = _next(4)
    return fri, fri + timedelta(days=3)


def _audit(client, action: str) -> list[AuditLog]:
    with client.SessionLocal() as db:
        return list(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all())


def _entries(client) -> list[TimeclockEntry]:
    with client.SessionLocal() as db:
        return list(db.execute(select(TimeclockEntry).order_by(TimeclockEntry.clock_in_at)).scalars().all())


@pytest.fixture()
def tech():
    c = _client()
    _seed(c)
    yield c
    _teardown(c)


# ── request ──────────────────────────────────────────────────────────────────


def test_a_tech_requests_and_the_office_approves_the_workdays_only(tech):
    fri, mon = _fri_to_mon()
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "long weekend",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["technician_id"] == TECH
    assert body["technician_name"] == "Amber Rosa"
    assert body["entry_type"] == "vacation"
    assert body["minutes_per_day"] == 480
    assert body["workday_count"] == 2, "Fri + Mon; Sat/Sun are not workdays"
    assert [a.user_id for a in _audit(tech, "time_off_requested")] == [TECH]
    assert _entries(tech) == [], "a pending request never touches the timeclock"

    # The tech sees it; nobody else's shows up.
    with tech.SessionLocal() as db:
        db.add(TimeOffRequest(
            id="other", company_id=TENANT, technician_id=OTHER_TECH, entry_type="vacation",
            start_date=fri, end_date=fri, minutes_per_day=480, status="pending", requested_by=OTHER_TECH,
        ))
        db.commit()
    mine = tech.get("/api/timeclock/time-off/requests").json()
    assert [m["id"] for m in mine] == [body["id"]]
    assert tech.get("/api/timeclock/time-off/requests?all_technicians=true").status_code == 403

    # The office sees both and approves ours.
    _as(tech, OFFICE, "dispatcher")
    everyone = tech.get("/api/timeclock/time-off/requests?all_technicians=true").json()
    assert {e["id"] for e in everyone} == {body["id"], "other"}
    r = tech.post(f"/api/timeclock/time-off/requests/{body['id']}/approve", json={"note": "enjoy"})
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved"
    assert approved["created"] == 2 and approved["skipped_days"] == []
    assert approved["reviewed_by"] == OFFICE and approved["review_note"] == "enjoy"

    rows = _entries(tech)
    assert [e.entry_type for e in rows] == ["vacation", "vacation"]
    assert [shop_day_of(e.clock_in_at, TZ) for e in rows] == [fri, mon]
    assert all(e.minutes == 480 and e.clock_out_at and e.notes == "long weekend" for e in rows)
    assert all(e.technician_id == TECH for e in rows)
    assert sorted(approved["entry_ids"]) == sorted(e.id for e in rows)
    trail = _audit(tech, "time_off_approved")
    assert len(trail) == 1 and trail[0].user_id == OFFICE
    assert sorted(trail[0].details["entry_ids"]) == sorted(e.id for e in rows)

    # Approving again is refused, not silently re-run.
    r = tech.post(f"/api/timeclock/time-off/requests/{body['id']}/approve")
    assert r.status_code == 409
    assert len(_entries(tech)) == 2


def test_a_tech_must_say_why_and_may_not_request_for_someone_else(tech):
    fri, mon = _fri_to_mon()
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(),
    })
    assert r.status_code == 422 and "note" in r.json()["detail"]
    r = tech.post("/api/timeclock/time-off/requests", json={
        "technician_id": OTHER_TECH, "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "x",
    })
    assert r.status_code == 403
    assert _audit(tech, "time_off_requested") == []


def test_a_viewer_cannot_request_but_the_office_may_request_on_behalf(tech):
    fri, mon = _fri_to_mon()
    _as(tech, OTHER_TECH, "viewer")
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "x",
    })
    assert r.status_code == 403, r.text

    _as(tech, OFFICE, "dispatcher")
    r = tech.post("/api/timeclock/time-off/requests", json={
        "technician_id": TECH, "start_date": fri.isoformat(), "end_date": mon.isoformat(),
    })
    assert r.status_code == 201, r.text
    assert r.json()["requested_by"] == OFFICE and r.json()["technician_id"] == TECH


def test_ranges_with_no_workdays_backdated_or_overlapping_are_refused(tech):
    sat = _next(5)
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": sat.isoformat(), "end_date": (sat + timedelta(days=1)).isoformat(), "notes": "x",
    })
    assert r.status_code == 422 and "no workdays" in r.json()["detail"]

    old = shop_today(TZ) - timedelta(days=30)
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": old.isoformat(), "end_date": old.isoformat(), "notes": "x",
    })
    assert r.status_code == 422 and "office" in r.json()["detail"]

    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": "2026-01-05", "end_date": "2026-01-01", "notes": "x",
    })
    assert r.status_code == 422

    fri, mon = _fri_to_mon()
    assert tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "x",
    }).status_code == 201
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": mon.isoformat(), "end_date": (mon + timedelta(days=2)).isoformat(), "notes": "x",
    })
    assert r.status_code == 409, r.text

    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "x", "minutes_per_day": 600,
    })
    assert r.status_code in (409, 422)


def test_a_tech_cannot_approve_and_the_office_can_deny_with_a_note(tech):
    fri, mon = _fri_to_mon()
    rid = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "x",
    }).json()["id"]
    assert tech.post(f"/api/timeclock/time-off/requests/{rid}/approve").status_code == 403
    assert tech.post(f"/api/timeclock/time-off/requests/{rid}/deny").status_code == 403

    _as(tech, OFFICE, "admin")
    r = tech.post(f"/api/timeclock/time-off/requests/{rid}/deny", json={"note": "short-staffed"})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert r.json()["review_note"] == "short-staffed"
    assert _entries(tech) == []
    assert [a.user_id for a in _audit(tech, "time_off_denied")] == [OFFICE]
    assert tech.post(f"/api/timeclock/time-off/requests/{rid}/approve").status_code == 409
    assert tech.post("/api/timeclock/time-off/requests/nope/approve").status_code == 404


def test_cancel_is_the_requesters_while_pending_and_nobody_elses(tech):
    fri, mon = _fri_to_mon()
    rid = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "notes": "x",
    }).json()["id"]
    _as(tech, OTHER_TECH, "technician")
    assert tech.post(f"/api/timeclock/time-off/requests/{rid}/cancel").status_code == 403
    _as(tech, TECH, "technician")
    r = tech.post(f"/api/timeclock/time-off/requests/{rid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert tech.post(f"/api/timeclock/time-off/requests/{rid}/cancel").status_code == 409
    assert [a.entity_id for a in _audit(tech, "time_off_cancelled")] == [rid]


def test_revoke_removes_only_what_the_approval_created(tech):
    fri, mon = _fri_to_mon()
    rid = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": fri.isoformat(), "end_date": fri.isoformat(), "notes": "x",
    }).json()["id"]
    # A real shift on the same day, entered before the office ruled.
    with tech.SessionLocal() as db:
        db.add(TimeclockEntry(
            id="worked", tenant_id=TENANT, technician_id=TECH,
            clock_in_at=f"{fri.isoformat()}T13:00:00+00:00", clock_out_at=f"{fri.isoformat()}T17:00:00+00:00",
            minutes=240, entry_type="clock", created_at="x", updated_at="x",
        ))
        db.commit()
    _as(tech, OFFICE, "manager")
    assert tech.post(f"/api/timeclock/time-off/requests/{rid}/revoke").status_code == 409, "not approved yet"
    approved = tech.post(f"/api/timeclock/time-off/requests/{rid}/approve").json()
    assert approved["created"] == 1
    r = tech.post(f"/api/timeclock/time-off/requests/{rid}/revoke", json={"note": "came in after all"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked" and r.json()["removed"] == 1
    live = [e for e in _entries(tech) if not e.deleted_at]
    assert [e.id for e in live] == ["worked"], "the worked shift survives; the vacation day is gone"
    trail = _audit(tech, "time_off_revoked")
    assert trail[0].details["entry_ids_removed"] == approved["entry_ids"]


# ── office direct entry ──────────────────────────────────────────────────────


def test_office_direct_entry_expands_workdays_and_is_idempotent(tech):
    fri, mon = _fri_to_mon()
    assert tech.post("/api/timeclock/time-off/entries", json={
        "technician_id": TECH, "entry_type": "vacation",
        "start_date": fri.isoformat(), "end_date": mon.isoformat(),
    }).status_code == 403, "a tech does not write time off directly"

    _as(tech, OFFICE, "dispatcher")
    r = tech.post("/api/timeclock/time-off/entries", json={
        "technician_id": TECH, "entry_type": "vacation",
        "start_date": fri.isoformat(), "end_date": mon.isoformat(), "minutes_per_day": 240, "notes": "half days",
    })
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 2 and r.json()["skipped_days"] == []
    rows = _entries(tech)
    assert [e.minutes for e in rows] == [240, 240]
    assert [a.user_id for a in _audit(tech, "time_off_entries_created")] == [OFFICE]

    r = tech.post("/api/timeclock/time-off/entries", json={
        "technician_id": TECH, "entry_type": "holiday",
        "start_date": fri.isoformat(), "end_date": mon.isoformat(),
    })
    assert r.status_code == 201
    assert r.json()["created"] == 0
    assert r.json()["skipped_days"] == [fri.isoformat(), mon.isoformat()]
    assert len(_entries(tech)) == 2

    r = tech.post("/api/timeclock/time-off/entries", json={
        "technician_id": TECH, "entry_type": "clock",
        "start_date": fri.isoformat(), "end_date": mon.isoformat(),
    })
    assert r.status_code == 422


# ── the shift-edit route ─────────────────────────────────────────────────────


def test_a_tech_cannot_stretch_an_approved_day_through_the_shift_editor():
    from gdx_dispatch.routers import timeclock as timeclock_mod

    c = _build_client(timeclock_mod.router, user=_login(TECH, "technician"), module_keys=("timeclock",))
    try:
        fri = _next(4)
        with c.SessionLocal() as db:
            db.add(TimeclockEntry(
                id="vac", tenant_id=TENANT, technician_id=TECH,
                clock_in_at=f"{fri.isoformat()}T13:00:00+00:00", clock_out_at=f"{fri.isoformat()}T21:00:00+00:00",
                minutes=480, entry_type="vacation", created_at="x", updated_at="x",
            ))
            db.commit()
        r = c.patch("/api/timeclock/entries/vac", json={
            "clock_out_at": f"{fri.isoformat()}T23:00:00+00:00", "notes": "longer",
        })
        assert r.status_code == 403, r.text
        _as(c, OFFICE, "dispatcher")
        r = c.patch("/api/timeclock/entries/vac", json={"clock_out_at": f"{fri.isoformat()}T17:00:00+00:00"})
        assert r.status_code == 200, r.text
        assert r.json()["minutes"] == 240
    finally:
        _teardown(c)


# ── holidays ─────────────────────────────────────────────────────────────────


def test_holidays_list_and_post_skip_people_who_already_hold_the_day():
    c = _client()
    xmas = date(shop_today(TZ).year, 12, 25)
    _seed(c, calendar=[{"date": xmas.isoformat(), "name": "Christmas Day", "minutes": 480}])
    try:
        r = c.get(f"/api/timeclock/time-off/holidays?start={xmas.year}-01-01&end={xmas.year}-12-31")
        assert r.status_code == 200
        assert r.json() == [{"date": xmas.isoformat(), "name": "Christmas Day", "minutes": 480, "posted": 0}]

        assert c.post("/api/timeclock/time-off/holidays/post", json={
            "date": xmas.isoformat(), "technician_ids": [TECH],
        }).status_code == 403, "techs do not post holidays"

        _as(c, OFFICE, "owner")
        r = c.post("/api/timeclock/time-off/holidays/post", json={
            "date": xmas.isoformat(), "technician_ids": [TECH, OTHER_TECH, TECH],
        })
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 2 and r.json()["skipped"] == []
        rows = _entries(c)
        assert sorted(e.technician_id for e in rows) == sorted([TECH, OTHER_TECH])
        assert all(e.entry_type == "holiday" and e.minutes == 480 and e.notes == "Christmas Day" for e in rows)
        assert all(shop_day_of(e.clock_in_at, TZ) == xmas for e in rows)
        trail = _audit(c, "holiday_posted")
        assert len(trail) == 1 and trail[0].user_id == OFFICE and trail[0].entity_id == xmas.isoformat()

        r = c.post("/api/timeclock/time-off/holidays/post", json={
            "date": xmas.isoformat(), "technician_ids": [TECH, OTHER_TECH],
        })
        assert r.json()["created"] == 0 and sorted(r.json()["skipped"]) == sorted([TECH, OTHER_TECH])
        assert len(_entries(c)) == 2

        r = c.get(f"/api/timeclock/time-off/holidays?start={xmas.year}-12-01&end={xmas.year}-12-31")
        assert r.json()[0]["posted"] == 2

        r = c.post("/api/timeclock/time-off/holidays/post", json={
            "date": f"{xmas.year}-12-26", "technician_ids": [TECH],
        })
        assert r.status_code == 422 and "calendar" in r.json()["detail"]
    finally:
        _teardown(c)


# ── options ──────────────────────────────────────────────────────────────────


def test_options_tell_the_form_the_day_length_and_the_policy_for_anyone_signed_in():
    c = _client()
    _seed(c, workdays=63, time_off_default_minutes=420, time_off_counts_toward_overtime=True)
    try:
        r = c.get("/api/timeclock/time-off/options")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["default_minutes"] == 420
        assert body["workdays"] == 63
        assert body["time_off_counts_toward_overtime"] is True
        assert body["requestable_types"] == ["vacation"]
        assert body["types"] == {"vacation": "Vacation", "holiday": "Holiday"}
        assert body["timezone"] == TZ
        _as(c, OTHER_TECH, "viewer")
        assert c.get("/api/timeclock/time-off/options").status_code == 200
    finally:
        _teardown(c)
