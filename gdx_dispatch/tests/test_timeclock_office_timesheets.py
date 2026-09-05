"""Office timesheets — reading and correcting OTHER people's shifts (2026-08-10).

Doug: "I don't think we have a way of looking at time sheets or fixing them.
We have the spot that says fix timeclock in the dispatch area but i couldn't
find a way of fixing them."

He was right. PATCH/POST /api/timeclock/entries had shipped permission-gated
and audit-logged with ZERO frontend callers, and Dispatch's "Fix" button
pushed to the SELF-SERVICE /timeclock page — which never read the entry id and
whose entries fetch defaults to the caller's own tech (`_resolve_tech_id`). The
office clicked Fix on a tech's broken shift and got their own timecard.

/timesheets is the page that closes that loop. Pinned here:

1. The self path still means "me" — P1-8's fix must not regress. This is the
   whole reason all_technicians is opt-in rather than a changed default.
2. all_technicians=true returns the crew, so a timesheet page is possible at all.
3. A tech cannot use it — it exposes other people's time (same gate as
   /exceptions and /payroll).
4. all_technicians ignores technician_id rather than half-applying it.
5. Correcting an open shift through PATCH clears it from /exceptions — the fix
   IS the dismissal, which is the contract the Dispatch card is built on.
6. A naive clock stamp does not 500. Comparing naive to aware raises TypeError;
   this endpoint had never been called by anything, so nothing had found it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import TimeclockBreak, TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import router

TENANT = "tenant-test"
OFFICE = "office-1"
_ROLE = {"value": "dispatcher"}


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    for ddl in (
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))""",
    ):
        setup.execute(text(ddl))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g2', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": OFFICE,
        "sub": OFFICE,
        "role": _ROLE["value"],
        "tenant_id": TENANT,
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(Session, *, entry_id: str, tech: str, hours_ago: float,
          minutes: int | None, closed: bool) -> str:
    clock_in = datetime.now(UTC) - timedelta(hours=hours_ago)
    db = Session()
    try:
        db.add(TimeclockEntry(
            id=entry_id,
            tenant_id=TENANT,
            technician_id=tech,
            clock_in_at=clock_in.isoformat(),
            clock_out_at=datetime.now(UTC).isoformat() if closed else None,
            minutes=minutes,
            notes=None,
            entry_type="clock",
            created_at=clock_in.isoformat(),
            updated_at=clock_in.isoformat(),
        ))
        db.commit()
    finally:
        db.close()
    return entry_id


def test_self_path_still_means_me(client):
    """P1-8 regression guard. all_technicians is opt-IN precisely so that the
    bare call keeps returning only the caller's rows — the bug P1-8 fixed was
    an admin seeing another tech's shift on their own timeclock."""
    tc, Session = client
    _seed(Session, entry_id="mine", tech=OFFICE, hours_ago=3, minutes=120, closed=True)
    _seed(Session, entry_id="theirs", tech="user-tech-a", hours_ago=3, minutes=120, closed=True)

    r = tc.get("/api/timeclock/entries")
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()] == ["mine"]


def test_all_technicians_returns_the_crew(client):
    """Without this there is no timesheet page — the endpoint could only ever
    show one person, and only if the caller already knew their user id."""
    tc, Session = client
    _seed(Session, entry_id="mine", tech=OFFICE, hours_ago=3, minutes=120, closed=True)
    _seed(Session, entry_id="theirs", tech="user-tech-a", hours_ago=3, minutes=120, closed=True)

    r = tc.get("/api/timeclock/entries", params={"all_technicians": "true"})
    assert r.status_code == 200, r.text
    assert {e["id"] for e in r.json()} == {"mine", "theirs"}


def test_technician_cannot_read_the_crew(client):
    """Same gate as /exceptions and /payroll: this exposes other people's time."""
    tc, Session = client
    _seed(Session, entry_id="theirs", tech="user-tech-a", hours_ago=3, minutes=120, closed=True)

    _ROLE["value"] = "technician"
    try:
        r = tc.get("/api/timeclock/entries", params={"all_technicians": "true"})
        assert r.status_code == 403, r.text
    finally:
        _ROLE["value"] = "dispatcher"


def test_technician_self_read_still_works(client):
    """The gate above must not cost a tech their own timecard."""
    tc, Session = client
    _seed(Session, entry_id="mine", tech=OFFICE, hours_ago=3, minutes=120, closed=True)

    _ROLE["value"] = "technician"
    try:
        r = tc.get("/api/timeclock/entries")
        assert r.status_code == 200, r.text
        assert [e["id"] for e in r.json()] == ["mine"]
    finally:
        _ROLE["value"] = "dispatcher"


def test_all_technicians_ignores_technician_id(client):
    """Honoring both would silently return one tech under a flag that says
    'everyone' — the caller asked for the crew."""
    tc, Session = client
    _seed(Session, entry_id="mine", tech=OFFICE, hours_ago=3, minutes=120, closed=True)
    _seed(Session, entry_id="theirs", tech="user-tech-a", hours_ago=3, minutes=120, closed=True)

    r = tc.get(
        "/api/timeclock/entries",
        params={"all_technicians": "true", "technician_id": "user-tech-a"},
    )
    assert r.status_code == 200, r.text
    assert {e["id"] for e in r.json()} == {"mine", "theirs"}


def test_office_corrects_an_open_shift_and_it_leaves_exceptions(client):
    """The end-to-end contract behind Dispatch's Fix button: the correction IS
    the dismissal. Before this page existed, nothing in the app could perform
    this PATCH."""
    tc, Session = client
    started = datetime.now(UTC) - timedelta(hours=40)
    _seed(Session, entry_id="open-1", tech="user-tech-a", hours_ago=40, minutes=None, closed=False)

    flagged = tc.get("/api/timeclock/exceptions")
    assert flagged.status_code == 200, flagged.text
    assert [e["entry_id"] for e in flagged.json()] == ["open-1"]
    assert flagged.json()[0]["kind"] == "open_shift"

    r = tc.patch(
        "/api/timeclock/entries/open-1",
        json={
            "clock_in_at": started.isoformat(),
            "clock_out_at": (started + timedelta(hours=8)).isoformat(),
            "notes": "Missed clock-out; end time confirmed with Michael.",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["minutes"] == 480

    assert tc.get("/api/timeclock/exceptions").json() == []


def test_correcting_an_unknown_duration_shift_clears_it(client):
    """The auto-close path leaves minutes NULL on purpose (the real end time is
    unknowable). Supplying it is the only way out, and only a human can."""
    tc, Session = client
    started = datetime.now(UTC) - timedelta(hours=30)
    _seed(Session, entry_id="auto-1", tech="user-tech-a", hours_ago=30, minutes=None, closed=True)

    assert tc.get("/api/timeclock/exceptions").json()[0]["kind"] == "unknown_duration_shift"

    r = tc.patch(
        "/api/timeclock/entries/auto-1",
        json={
            "clock_in_at": started.isoformat(),
            "clock_out_at": (started + timedelta(hours=7, minutes=30)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["minutes"] == 450
    assert tc.get("/api/timeclock/exceptions").json() == []


def test_naive_clock_stamp_is_a_422_not_a_500(client):
    """Stored stamps are tz-aware; a naive one from a client used to hit
    `naive <= aware` and raise TypeError -> 500. Nothing had ever called this
    endpoint, so nothing had found it."""
    tc, Session = client
    _seed(Session, entry_id="e-1", tech="user-tech-a", hours_ago=5, minutes=300, closed=True)

    # Naive AND backwards — the ordering check must reject it cleanly.
    r = tc.patch(
        "/api/timeclock/entries/e-1",
        json={"clock_out_at": "2020-01-01T08:00:00"},
    )
    assert r.status_code == 422, r.text

    # Naive and valid — normalized to UTC, not rejected and not a crash.
    r = tc.patch(
        "/api/timeclock/entries/e-1",
        json={
            "clock_in_at": "2026-08-10T08:00:00",
            "clock_out_at": "2026-08-10T16:30:00",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["minutes"] == 510


def test_office_adds_a_missed_shift(client):
    """A tech who never clocked in at all has no row to correct — the office
    needs to be able to create one."""
    tc, _ = client
    start = datetime.now(UTC) - timedelta(days=1, hours=8)
    r = tc.post(
        "/api/timeclock/entries",
        json={
            "technician_id": "user-tech-a",
            "clock_in_at": start.isoformat(),
            "clock_out_at": (start + timedelta(hours=8)).isoformat(),
            "notes": "Phone died; hours confirmed with the customer.",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["minutes"] == 480

    crew = tc.get("/api/timeclock/entries", params={"all_technicians": "true"}).json()
    assert [e["technician_id"] for e in crew] == ["user-tech-a"]


def test_crew_read_reports_break_minutes(client):
    """`minutes` is GROSS elapsed — clock-out writes _minutes_between(in, now)
    and never subtracts breaks, which live in their own table. Without this
    join the office timesheet pays out every lunch ever taken. Caught by an
    adversarial audit 2026-08-10; 9 real break rows carry a duration on the
    dev copy, so this was not hypothetical."""
    tc, Session = client
    _seed(Session, entry_id="shift", tech="user-tech-a", hours_ago=8, minutes=480, closed=True)
    mid_shift = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    db = Session()
    try:
        # time_entry_id deliberately NULL — that is the real-world shape. The
        # column is optional and both clients post `{}`, so it is NULL on 10 of
        # 10 prod-copy rows. A join on it would silently net nothing while
        # looking correct.
        db.add(TimeclockBreak(
            id="brk-1", tenant_id=TENANT, user_id="user-tech-a", time_entry_id=None,
            type="lunch", started_at=mid_shift,
            ended_at=mid_shift, duration_minutes=30,
            created_at=mid_shift,
        ))
        # An OPEN break has no defensible length — it must contribute nothing
        # rather than have one invented for it.
        db.add(TimeclockBreak(
            id="brk-2", tenant_id=TENANT, user_id="user-tech-a", time_entry_id=None,
            type="lunch", started_at=mid_shift,
            ended_at=None, duration_minutes=None,
            created_at=mid_shift,
        ))
        # A break from a shift outside this window must not be netted off.
        db.add(TimeclockBreak(
            id="brk-3", tenant_id=TENANT, user_id="user-tech-a", time_entry_id=None,
            type="lunch", started_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(),
            ended_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(), duration_minutes=45,
            created_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(),
        ))
        db.commit()
    finally:
        db.close()

    rows = tc.get("/api/timeclock/entries", params={"all_technicians": "true"}).json()
    shift = next(e for e in rows if e["id"] == "shift")
    assert shift["minutes"] == 480, "gross elapsed must stay untouched"
    assert shift["break_minutes"] == 30, (
        "only the ENDED break INSIDE this shift's window counts — not the open "
        "one (no defensible length) and not the one from a month ago"
    )


def test_self_read_is_unchanged_by_the_break_join(client):
    """The join is office-view only. The self-service page's callers do not
    expect the field and must not have their payload shape shift under them."""
    tc, Session = client
    _seed(Session, entry_id="mine", tech=OFFICE, hours_ago=8, minutes=480, closed=True)

    rows = tc.get("/api/timeclock/entries").json()
    assert rows[0]["break_minutes"] is None


def test_roster_includes_everyone_with_entries(client):
    """The roster exists because /api/users is the wrong source: it excludes
    soft-deleted people. Found in the browser 2026-08-10 — Dispatch's card named
    a departed employee that /api/users could not, so the timesheet rendered
    "Unknown (<user-id>…)" against her real hours. Reviewing a former employee's
    time is a large part of why anyone opens a timesheet."""
    tc, Session = client
    _seed(Session, entry_id="e-1", tech="user-tech-a", hours_ago=5, minutes=300, closed=True)
    _seed(Session, entry_id="e-2", tech="user-departed", hours_ago=9, minutes=420, closed=True)

    r = tc.get("/api/timeclock/roster")
    assert r.status_code == 200, r.text
    with_entries = {i["technician_id"] for i in r.json() if i["has_entries"]}
    assert with_entries == {"user-tech-a", "user-departed"}


def test_roster_is_dispatch_only(client):
    """It enumerates who works here — same tier as the rest of this page."""
    tc, _ = client
    _ROLE["value"] = "technician"
    try:
        assert tc.get("/api/timeclock/roster").status_code == 403
    finally:
        _ROLE["value"] = "dispatcher"


def test_roster_keeps_unmatched_ids(client):
    """An id matching no user row still gets an entry (unnamed) rather than
    being dropped — 10 of 39 prod rows match neither users nor technicians, and
    those hours are still on the books."""
    tc, Session = client
    _seed(Session, entry_id="orphan", tech="ghost-id", hours_ago=5, minutes=300, closed=True)

    rows = tc.get("/api/timeclock/roster").json()
    ghost = [i for i in rows if i["technician_id"] == "ghost-id"]
    assert len(ghost) == 1
    assert ghost[0]["name"] is None
    assert ghost[0]["has_entries"] is True


def test_technician_cannot_edit_another_techs_entry(client):
    """The write gate is per-entry, not just per-page."""
    tc, Session = client
    _seed(Session, entry_id="theirs", tech="user-tech-a", hours_ago=5, minutes=300, closed=True)

    _ROLE["value"] = "technician"
    try:
        r = tc.patch("/api/timeclock/entries/theirs", json={"notes": "nope"})
        assert r.status_code == 403, r.text
    finally:
        _ROLE["value"] = "dispatcher"
