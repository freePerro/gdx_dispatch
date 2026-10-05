"""GDXA-174 — /api/performance/users reads hours from the hours authority, and
says "unavailable" in words instead of a plausible 0.

Before: the hours read was `SUM(hours_worked) FROM timeclock_entries WHERE
company_id = ... AND user_id = ...`. That table and every one of those columns
exist nowhere, so it raised on every request and `hours_worked` was 0.0 for
everyone, forever. Every stat also started at 0, so a read that failed was
indistinguishable from a real nought.

Behavioural: every test runs the real handler against a real session built
from the ORM. The failure case drops the real table rather than mocking the
reader, because a mock proves which arguments were passed, never which value
comes back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import AppSettings, Invoice, Job, Technician, User
from gdx_dispatch.routers import performance as perf
from gdx_dispatch.routers.auth import get_current_user

_TENANT = "tenant-a"
_PERIOD = "2026-03"
_OFFICE = {"user_id": "office-1", "role": "admin", "tenant_id": _TENANT}


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": _TENANT}
    return req


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'performance.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    # UTC so the shop-day bucketing in these fixtures is the calendar day as
    # written; the bucketing itself is pinned in test_timesheet_hours.
    session.add(AppSettings(timezone="UTC"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _user(db, name: str) -> str:
    uid = uuid4()
    db.add(User(id=uid, name=name, company_id=_TENANT, role="technician"))
    db.commit()
    # The timeclock writes `technician_id` from the token's `sub`: a dashed
    # string. SQLite keeps `User.id` as dashless hex — the join has to survive
    # both spellings, which is exactly what this fixture exercises.
    return str(uid)


def _shift(db, tech: str, clock_in: str, minutes: int | None, *, deleted: bool = False,
           entry_type: str = "clock", clocked_out: bool = True) -> str:
    """`clocked_out=False` is a shift still open; `minutes=None` with a
    clock-out is one the clock lost the duration of."""
    entry_id = uuid4().hex
    clock_out = (
        (datetime.fromisoformat(clock_in) + timedelta(minutes=minutes or 60)).isoformat()
        if clocked_out else None
    )
    if not clocked_out:
        minutes = None
    db.execute(
        text(
            "INSERT INTO timeclock_entries_router"
            " (id, tenant_id, technician_id, entry_type, clock_in_at, clock_out_at,"
            "  minutes, created_at, updated_at, deleted_at)"
            " VALUES (:id, :t, :u, :ty, :i, :o, :m, :i, :i, :d)"
        ),
        {
            "id": entry_id, "t": _TENANT, "u": tech, "ty": entry_type, "i": clock_in,
            "o": clock_out, "m": minutes, "d": clock_in if deleted else None,
        },
    )
    db.commit()
    return entry_id


def _break(db, tech: str, started_at: str, minutes: int) -> None:
    db.execute(
        text(
            "INSERT INTO timeclock_breaks_router"
            " (id, tenant_id, user_id, type, started_at, ended_at, duration_minutes,"
            "  created_at)"
            " VALUES (:id, :t, :u, 'lunch', :s, :s, :m, :s)"
        ),
        {"id": uuid4().hex, "t": _TENANT, "u": tech, "s": started_at, "m": minutes},
    )
    db.commit()


def _all(db, period: str | None = _PERIOD, user: dict | None = None) -> dict[str, dict]:
    out = perf.all_users_performance(
        request=_request(), user=user or _OFFICE, db=db, period=period
    )
    return {u["id"]: u for u in out["users"]}


def test_hours_are_worked_clock_hours_from_the_authority(db):
    """Breaks netted, soft-deletes and other months excluded, time off not worked."""
    tech = _user(db, "Alex")
    # An 8h shift with a 30m lunch inside it, and a 4h shift: 11.5 worked.
    _shift(db, tech, "2026-03-10T13:00:00+00:00", 480)
    _break(db, tech, "2026-03-10T17:00:00+00:00", 30)
    _shift(db, tech, "2026-03-11T13:00:00+00:00", 240)
    # None of these are worked hours in March.
    _shift(db, tech, "2026-03-12T13:00:00+00:00", 600, deleted=True)
    _shift(db, tech, "2026-04-01T13:00:00+00:00", 480)
    _shift(db, tech, "2026-03-13T13:00:00+00:00", 480, entry_type="vacation")

    row = _all(db)[tech]
    # The difference is the assertion: summing raw `minutes` gives 12.0 (pays
    # the lunch), counting the soft-delete gives 21.5, the vacation day 19.5.
    assert row["stats"]["hours_worked"] == 11.5
    assert "hours_worked" not in row["unavailable"]


def test_detail_endpoint_reports_the_same_hours(db):
    tech = _user(db, "Alex")
    _shift(db, tech, "2026-03-10T13:00:00+00:00", 300)
    out = perf.user_performance(
        user_id=tech, request=_request(), user=_OFFICE, db=db, period=_PERIOD
    )
    assert out["stats"]["hours_worked"] == 5.0
    assert out["unavailable"].get("hours_worked") is None


def test_a_tech_with_no_hours_and_no_jobs_reads_as_no_data_not_zero(db):
    """The empty-state path the maintainer asked for: never a nought that reads
    as "did no work" when nothing was recorded at all."""
    worked = _user(db, "Alex")
    idle = _user(db, "Blair")
    _shift(db, worked, "2026-03-10T13:00:00+00:00", 240)

    row = _all(db)[idle]
    assert row["stats"]["hours_worked"] is None
    assert row["unavailable"]["hours_worked"] == perf.REASON_NO_DATA
    # An average over no jobs does not exist; it is not $0.00.
    assert row["stats"]["avg_job_value"] is None
    assert row["unavailable"]["avg_job_value"] == perf.REASON_NO_DATA
    # A count that was actually taken stays a real count.
    assert row["stats"]["jobs_completed"] == 0
    assert "jobs_completed" not in row["unavailable"]


def test_an_empty_period_has_every_hours_figure_unavailable(db):
    tech = _user(db, "Alex")
    _shift(db, tech, "2026-04-02T13:00:00+00:00", 240)
    row = _all(db, "2026-03")[tech]
    assert row["stats"]["hours_worked"] is None
    assert row["unavailable"]["hours_worked"] == perf.REASON_NO_DATA


def test_no_month_means_hours_unavailable_not_an_all_time_guess(db):
    tech = _user(db, "Alex")
    _shift(db, tech, "2026-03-10T13:00:00+00:00", 240)
    row = _all(db, None)[tech]
    assert row["stats"]["hours_worked"] is None
    assert row["unavailable"]["hours_worked"] == perf.REASON_PERIOD_REQUIRED


def test_an_unreadable_timeclock_is_unavailable_and_spares_the_other_stats(db):
    """The defect's own shape: a read that cannot run must not render as 0.0."""
    tech = _user(db, "Alex")
    db.execute(text("DROP TABLE timeclock_entries_router"))
    db.commit()

    row = _all(db)[tech]
    assert row["stats"]["hours_worked"] is None
    assert row["unavailable"]["hours_worked"] == perf.REASON_READ_FAILED
    # The reads after it still ran.
    assert row["stats"]["tasks_completed"] == 0
    assert row["stats"]["safety_checklists"] == 0


def test_a_shift_the_timesheet_flags_makes_the_month_unknown_not_zero(db):
    """Payroll counts a shift with no known length as 0 beside its flag; this
    page has no flag column, so 0.0 or an undercount would read as fact."""
    forgot = _user(db, "Alex")  # never clocked out, weeks ago: `open_shift`
    _shift(db, forgot, "2026-03-10T13:00:00+00:00", 240)
    _shift(db, forgot, "2026-03-11T13:00:00+00:00", None, clocked_out=False)
    lost = _user(db, "Blair")  # clocked out, duration lost: `unknown_duration`
    _shift(db, lost, "2026-03-12T13:00:00+00:00", None)
    huge = _user(db, "Casey")  # 72h on one clock-out: `implausible` (prod has such rows)
    _shift(db, huge, "2026-03-13T13:00:00+00:00", 480)
    _shift(db, huge, "2026-03-14T13:00:00+00:00", 72 * 60)

    rows = _all(db)
    for tech in (forgot, lost, huge):
        assert rows[tech]["stats"]["hours_worked"] is None
        assert rows[tech]["unavailable"]["hours_worked"] == perf.REASON_SHIFT_FLAGGED


def test_a_tech_on_the_clock_right_now_is_not_blanked_or_zeroed(db):
    """Open and unflagged is a shift in progress: normal every workday, and
    the page opens on the current month. Finished shifts still count."""
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    period = started.strftime("%Y-%m")
    first = started.replace(day=1, hour=0, minute=0, second=0)
    working = _user(db, "Alex")
    _shift(db, working, first.isoformat(), 90)
    _shift(db, working, started.isoformat(), None, clocked_out=False)
    first_day = _user(db, "Blair")
    _shift(db, first_day, started.isoformat(), None, clocked_out=False)

    rows = _all(db, period)
    assert rows[working]["stats"]["hours_worked"] == 1.5
    assert "hours_worked" not in rows[working]["unavailable"]
    assert rows[first_day]["stats"]["hours_worked"] is None
    assert rows[first_day]["unavailable"]["hours_worked"] == perf.REASON_IN_PROGRESS


@pytest.mark.parametrize("role", ["sales", "accounting", "viewer"])
def test_office_roles_outside_dispatch_do_not_see_the_crews_hours(db, role):
    """`nav.office` admits roles the timesheet refuses; the hours say so in
    words rather than leaking, and rather than reading as 0."""
    tech = _user(db, "Alex")
    _shift(db, tech, "2026-03-10T13:00:00+00:00", 240)
    row = _all(db, user={**_OFFICE, "role": role})[tech]
    assert row["stats"]["hours_worked"] is None
    assert row["unavailable"]["hours_worked"] == perf.REASON_RESTRICTED
    # Commission is pay-adjacent: withheld from the same roles, in words.
    assert row["stats"]["commission_earned"] is None
    assert row["unavailable"]["commission_earned"] == perf.REASON_RESTRICTED
    # A dispatch-tier reader of the same data gets the number.
    manager_row = _all(db, user={**_OFFICE, "role": "dispatcher"})[tech]
    assert manager_row["stats"]["hours_worked"] == 4.0
    assert manager_row["unavailable"].get("commission_earned") != perf.REASON_RESTRICTED


def test_a_job_assigned_the_normal_way_counts_for_its_tech(db):
    """`jobs.assigned_to` holds a technician id (core/job_access.py); a read
    matching only the user id answered 0 for every normally assigned job."""
    tech = _user(db, "Alex")
    tech_row = Technician(company_id=_TENANT, user_id=tech, name="Alex")
    db.add(tech_row)
    db.flush()
    march = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    db.add_all([
        Job(title="via technician", company_id=_TENANT, assigned_to=tech_row.id,
            status="Completed", created_at=march),
        Job(title="legacy direct", company_id=_TENANT, assigned_to=tech,
            status="Completed", created_at=march),
        Job(title="deleted", company_id=_TENANT, assigned_to=tech_row.id,
            status="Completed", created_at=march, deleted_at=march),
        Job(title="open", company_id=_TENANT, assigned_to=tech_row.id,
            status="Scheduled", created_at=march),
    ])
    db.commit()

    row = _all(db)[tech]
    assert row["stats"]["jobs_completed"] == 2
    assert "jobs_completed" not in row["unavailable"]


def test_revenue_counts_only_invoices_that_finalize_billing(db):
    """Void, soft-deleted and draft invoices are not revenue earned: a
    voided-and-reissued invoice must not count twice. Sent counts (invoiced,
    not necessarily paid), and only on the jobs counted as completed, since
    Avg Job divides one by the other. A deposit COUNTS — the final invoice
    nets it with a negative line, so dropping it would subtract it twice
    (the rule `routers/reports.py` already applies)."""
    tech = _user(db, "Alex")
    tech_row = Technician(company_id=_TENANT, user_id=tech, name="Alex")
    db.add(tech_row)
    db.flush()
    march = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    job = Job(title="door", company_id=_TENANT, assigned_to=tech_row.id,
              status="Completed", created_at=march)
    unfinished = Job(title="half done", company_id=_TENANT, assigned_to=tech_row.id,
                     status="In Progress", created_at=march)
    db.add_all([job, unfinished])
    db.flush()
    customer = uuid4()
    db.add(Invoice(invoice_number="INV-P", public_token=uuid4().hex, customer_id=customer,
                   company_id=_TENANT, job_id=unfinished.id, status="sent", total=5000,
                   billing_type="progress", created_at=march))
    for n, (status, extra) in enumerate([
        ("paid", {}),
        ("sent", {}),
        ("void", {}),
        ("paid", {"deleted_at": march}),
        ("draft", {}),
        ("sent", {"billing_type": "deposit"}),
    ]):
        db.add(Invoice(invoice_number=f"INV-{n}", public_token=uuid4().hex,
                       customer_id=customer, company_id=_TENANT, job_id=job.id,
                       status=status, total=100, created_at=march, **extra))
    db.commit()

    stats = _all(db)[tech]["stats"]
    assert stats["jobs_completed"] == 1
    assert stats["revenue"] == 300.0
    assert stats["avg_job_value"] == 300.0
    # The detail endpoint answers the same for any spelling of the id.
    shouty = tech.replace("-", "").upper()
    detail = perf.user_performance(
        user_id=shouty, request=_request(), user=_OFFICE, db=db, period=_PERIOD
    )
    assert detail["stats"]["revenue"] == 300.0


def test_a_job_counts_in_the_month_it_was_finished_not_booked(db):
    """An installation booked in February and finished and billed in March is
    March's work. Windowing on `jobs.created_at` showed March as 0 jobs and
    $0.00 for a tech who did the work — a real-looking zero."""
    tech = _user(db, "Alex")
    tech_row = Technician(company_id=_TENANT, user_id=tech, name="Alex")
    db.add(tech_row)
    db.flush()
    booked = datetime(2026, 2, 25, 15, 0, tzinfo=UTC)
    finished = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    job = Job(title="install", company_id=_TENANT, assigned_to=tech_row.id,
              status="Completed", created_at=booked, completed_at=finished)
    db.add(job)
    db.flush()
    db.add(Invoice(invoice_number="INV-I", public_token=uuid4().hex, customer_id=uuid4(),
                   company_id=_TENANT, job_id=job.id, status="paid", total=100,
                   created_at=finished))
    db.commit()

    march = _all(db, period="2026-03")[tech]["stats"]
    assert march["jobs_completed"] == 1
    assert march["revenue"] == 100.0
    february = _all(db, period="2026-02")[tech]
    assert february["stats"]["jobs_completed"] == 0
    assert february["unavailable"]["avg_job_value"] == perf.REASON_NO_DATA


def test_month_end_evening_work_lands_in_the_same_month_as_its_hours(db):
    """The counts use the shop's month, the one the hours authority uses. A
    job finished at 8pm Central on 31 March is 01:00Z on 1 April: on a UTC
    window it went to April while that evening's shift stayed in March."""
    db.query(AppSettings).update({"timezone": "America/Chicago"})
    db.commit()
    tech = _user(db, "Alex")
    tech_row = Technician(company_id=_TENANT, user_id=tech, name="Alex")
    db.add(tech_row)
    db.flush()
    _shift(db, tech, "2026-03-31T23:00:00+00:00", 180)  # 18:00-21:00 CDT
    finished = datetime(2026, 4, 1, 1, 0, tzinfo=UTC)  # 20:00 CDT, 31 Mar
    db.add(Job(title="late call", company_id=_TENANT, assigned_to=tech_row.id,
               status="Completed", created_at=finished, completed_at=finished))
    db.commit()

    march = _all(db, period="2026-03")[tech]["stats"]
    assert march["hours_worked"] == 3.0
    assert march["jobs_completed"] == 1
    assert _all(db, period="2026-04")[tech]["stats"]["jobs_completed"] == 0


def test_estimates_are_named_not_recorded_rather_than_counted_as_zero(db):
    """`estimates` records no staff author, so the count cannot be taken."""
    tech = _user(db, "Alex")
    row = _all(db)[tech]
    assert row["stats"]["estimates_created"] is None
    assert row["unavailable"]["estimates_created"] == perf.REASON_NOT_RECORDED
    assert row["unavailable"]["estimates_accepted"] == perf.REASON_NOT_RECORDED


# ── HTTP: the period contract and the permission gate ─────────────────────────


def _client(db, perms: set[str]) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _state(request, call_next):
        request.state.tenant = {"id": _TENANT}
        # `require_permission` reads this cache before touching the database;
        # setting it runs the REAL gate against a known permission set.
        request.state.user_permissions = perms
        return await call_next(request)

    app.include_router(perf.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _OFFICE
    # Only the module gate is bypassed; the permission gate stays real.
    module_gate = perf.router.dependencies[0].dependency
    app.dependency_overrides[module_gate] = lambda: None
    return TestClient(app)


def test_a_malformed_month_is_refused_not_silently_widened(db):
    """The page used to send `?start=&end=`, which FastAPI dropped, so the date
    picker filtered nothing. A bad `period` is now a 422, not all-time."""
    client = _client(db, {"nav.office"})
    assert client.get("/api/performance/users", params={"period": "2026-13"}).status_code == 422
    assert client.get("/api/performance/users", params={"period": "March"}).status_code == 422
    assert client.get("/api/performance/users", params={"period": _PERIOD}).status_code == 200


def test_a_technician_cannot_read_the_crews_hours(db):
    """The page is office-only; which office roles see hours is pinned above."""
    _user(db, "Alex")
    tech_client = _client(db, {"jobs.read"})
    assert tech_client.get("/api/performance/users", params={"period": _PERIOD}).status_code == 403
    office_client = _client(db, {"nav.office"})
    assert office_client.get("/api/performance/users", params={"period": _PERIOD}).status_code == 200
