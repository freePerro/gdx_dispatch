"""The two files that get emailed to the bookkeeper.

Named for the failures, not the features:

* The CSV and the PDF disagreeing about somebody's hours. They are built
  from one `PeriodTimesheet`, and `test_the_two_files_agree` is what keeps
  that true rather than merely intended.
* A file that omits its own unresolved rows. An open shift dropped from the
  export reads as a day off and under-pays a person; both files must name it.
* Times printed in UTC. 13:06 UTC is 8:06 AM in the shop, and a bookkeeper
  reading "13:06" for a morning start will "correct" it.
* An export endpoint reachable by a technician, which is everyone else's
  hours.
* A read failure returning an empty timesheet — a payroll file reporting
  zero hours it never queried is worse than no file at all.
"""
from __future__ import annotations

import csv
import io
import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.pay_periods import PayPeriod
from gdx_dispatch.core.timesheet_export import (
    CSV_HEADER,
    build_csv,
    csv_filename,
    pdf_context,
    pdf_filename,
)
from gdx_dispatch.core.timesheet_hours import build_timesheet
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockBreak, TimeclockEntry
from gdx_dispatch.routers import timeclock as timeclock_router_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import router as timeclock_router

TENANT = "tenant-test"
TZ = "America/Chicago"
MICHAEL = "user-michael"
AMBER = "user-amber"
PERIOD = PayPeriod(date(2026, 8, 10), date(2026, 8, 23))
NAMES = {MICHAEL: "Michael Tallman", AMBER: "Amber Joy Rosa"}


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _shift(db: Session, *, entry_id, tech=MICHAEL, clock_in, clock_out=None,
           minutes=480, notes=None):
    db.add(TimeclockEntry(
        id=entry_id, tenant_id=TENANT, technician_id=tech,
        clock_in_at=clock_in, clock_out_at=clock_out, minutes=minutes,
        notes=notes, entry_type="clock", created_at=clock_in, updated_at=clock_in,
    ))
    db.commit()


def _sheet(db: Session):
    return build_timesheet(
        db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES
    )


def _rows(text_csv: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text_csv)))


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def test_csv_carries_a_total_row_per_person(db: Session):
    """The total is what actually gets keyed in. It is labelled TOTAL so the
    detail rows above it are not keyed by mistake."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _shift(db, entry_id="e2", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T18:00:00+00:00", minutes=300)

    rows = _rows(build_csv(_sheet(db)))
    totals = [r for r in rows if r["date"] == "TOTAL"]
    assert len(totals) == 1
    assert totals[0]["worked_hours"] == "14.00"
    assert totals[0]["employee"] == "Michael Tallman"


def test_csv_header_is_stable(db: Session):
    """A bookkeeper's import maps columns by name. Reordering silently
    remaps somebody's hours onto the break column."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    first_line = build_csv(_sheet(db)).splitlines()[0]
    assert first_line == ",".join(CSV_HEADER)


def test_csv_prints_shop_time_not_utc(db: Session):
    """13:00 UTC is 8:00 AM in the shop. Printing 13:00 invites a correction
    that would be wrong."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    row = _rows(build_csv(_sheet(db)))[0]
    assert row["clock_in"] == "8:00 AM"
    assert row["clock_out"] == "5:00 PM"


def test_csv_names_a_shift_that_needs_review(db: Session):
    _shift(db, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)
    rows = _rows(build_csv(_sheet(db)))
    detail = [r for r in rows if r["date"] != "TOTAL"][0]
    assert detail["needs_review"] == "yes"
    assert detail["note"]
    total = [r for r in rows if r["date"] == "TOTAL"][0]
    assert total["needs_review"] == "yes", "the total must carry the doubt too"


def test_a_clean_period_flags_nothing(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    assert all(r["needs_review"] == "" for r in _rows(build_csv(_sheet(db))))


def test_csv_nets_breaks_off_the_total(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    db.add(TimeclockBreak(
        id="b1", tenant_id=TENANT, user_id=MICHAEL, type="lunch",
        started_at="2026-08-17T17:00:00+00:00", ended_at="2026-08-17T17:30:00+00:00",
        duration_minutes=30, created_at="2026-08-17T17:00:00+00:00",
    ))
    db.commit()
    total = [r for r in _rows(build_csv(_sheet(db))) if r["date"] == "TOTAL"][0]
    assert total["worked_hours"] == "8.50"
    assert total["break_minutes"] == "30"


def test_csv_has_no_money_columns(db: Session):
    """Hours are the deliverable; the bookkeeper applies rates. A rate column
    here would be this app inventing pay it was never told."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    header = build_csv(_sheet(db)).splitlines()[0].lower()
    for word in ("rate", "gross", "pay", "wage", "overtime", "amount"):
        assert word not in header


def test_csv_of_an_empty_period_is_a_header_only(db: Session):
    body = build_csv(_sheet(db))
    assert body.strip() == ",".join(CSV_HEADER)


def test_filenames_name_the_period(db: Session):
    assert csv_filename(PERIOD) == "timesheet_2026-08-10_2026-08-23.csv"
    assert pdf_filename(PERIOD) == "timesheet_2026-08-10_2026-08-23.pdf"


# ---------------------------------------------------------------------------
# PDF context
# ---------------------------------------------------------------------------

def test_pdf_lists_every_flagged_shift_at_the_top(db: Session):
    """Buried on page three is the same as omitted."""
    _shift(db, entry_id="e-open", clock_in="2026-08-11T13:00:00+00:00",
           clock_out=None, minutes=None)
    ctx = pdf_context(_sheet(db), pay_date="2026-08-28")
    assert len(ctx["flagged"]) == 1
    name, _day, reason = ctx["flagged"][0]
    assert name == "Michael Tallman"
    assert reason


def test_pdf_carries_the_pay_date(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    ctx = pdf_context(_sheet(db), pay_date="2026-08-28")
    assert ctx["pay_date"] == "2026-08-28"
    assert ctx["period"]["label"] == "2026-08-10 – 2026-08-23"


def test_the_two_files_agree(db: Session):
    """One timesheet, two renderings. If these ever diverge, the bookkeeper
    has two documents and no way to know which is right."""
    _shift(db, entry_id="e1", tech=MICHAEL, clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _shift(db, entry_id="e2", tech=AMBER, clock_in="2026-08-18T14:00:00+00:00",
           clock_out="2026-08-18T18:00:00+00:00", minutes=240)
    sheet = _sheet(db)

    ctx = pdf_context(sheet)
    csv_totals = {
        r["employee"]: r["worked_hours"]
        for r in _rows(build_csv(sheet)) if r["date"] == "TOTAL"
    }
    pdf_totals = {c["name"]: f"{c['hours']:.2f}" for c in ctx["cards"]}
    assert csv_totals == pdf_totals
    assert f"{ctx['total_hours']:.2f}" == "13.00"


def test_pdf_renders_for_real(db: Session):
    """WeasyPrint pulls a native stack; a template that renders in Jinja can
    still fail there, and the endpoint would hand back a broken download."""
    from gdx_dispatch.core.timesheet_export import build_pdf

    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _shift(db, entry_id="e-open", clock_in="2026-08-11T13:00:00+00:00",
           clock_out=None, minutes=None)
    pdf = build_pdf(
        _sheet(db),
        branding={"company_name": "Garage Door Xperts"},
        pay_date="2026-08-28",
        prepared_at="Aug 24, 2026 7:00 AM",
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@contextmanager
def _export_app(*, refuse_audit: bool = False):
    """The export endpoints on a throwaway SQLite database.

    Yields `(TestClient, SessionLocal, handler_sessions)`. `handler_sessions`
    collects every session handed to a handler, so a test can inspect what a
    failed audit write left behind on the session the handler was still using.

    `refuse_audit=True` installs a real `BEFORE INSERT ON audit_logs` trigger.
    The refusal has to come from the storage layer rather than a patched
    function: the defect this guards is what a failed *flush* does to the
    session, and only a real statement failure produces that. Same mechanism
    GDXA-44's triage used, and `tests/test_audit_best_effort.py` after it.

    The sessionmaker matches `core/database.py`'s arguments, `expire_on_commit`
    included. That is house-keeping, not a guard: measured for GDXA-58, pinning
    it `False` here changes no result, because nothing crossing the audit
    boundary in these handlers is ORM-mapped — `PeriodTimesheet` is a plain
    dataclass and `branding` a plain dict, so there is no identity to expire.
    The expire-on-commit trap is real at sites that read a row back through the
    session (see `test_audit_best_effort.py`); it cannot reach this one.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = SessionLocal()
    for ddl in (
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))""",
    ):
        setup.execute(text(ddl))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)"
        " VALUES ('g2', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.add(AppSettings(
        company_name="Garage Door Xperts", address="", phone="", email="", logo="",
        timezone=TZ, enabled_modules=[], notification_preferences={}, integrations={},
        pay_period_cadence="biweekly", pay_period_anchor_start=date(2026, 8, 10),
        pay_period_pay_lag_days=5,
    ))
    setup.commit()
    setup.close()

    if refuse_audit:
        # `audit_logs` is created lazily by `ensure_audit_table`, so the table
        # has to exist before a trigger can be hung on it.
        ensure_audit_table(SessionLocal())
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TRIGGER audit_logs_refuse_insert BEFORE INSERT ON audit_logs "
                "BEGIN SELECT RAISE(ABORT, 'audit storage refuses this row'); END;"
            ))

    handler_sessions: list[Session] = []

    def _override_db():
        session = SessionLocal()
        handler_sessions.append(session)
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(timeclock_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "office-1", "sub": "office-1", "role": "dispatcher", "tenant_id": TENANT,
    }
    try:
        yield TestClient(app, raise_server_exceptions=True), SessionLocal, handler_sessions
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture()
def client():
    with _export_app() as (tc, SessionLocal, _sessions):
        yield tc, SessionLocal


@pytest.fixture()
def refusing_client():
    """A client whose every audit write is refused by the database itself."""
    with _export_app(refuse_audit=True) as parts:
        yield parts


def _seed(SessionLocal, **kw):
    session = SessionLocal()
    try:
        _shift(session, **kw)
    finally:
        session.close()


def test_csv_endpoint_returns_an_attachment(client):
    tc, SessionLocal = client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    r = tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "timesheet_2026-08-10_2026-08-23.csv" in r.headers["content-disposition"]
    assert "TOTAL" in r.text


def test_pdf_endpoint_returns_a_real_pdf(client):
    tc, SessionLocal = client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    r = tc.get("/api/timeclock/pay-period/export.pdf?start=2026-08-10&end=2026-08-23")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_a_technician_cannot_export_the_crews_hours(client):
    tc, _ = client
    tc.app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "tech-9", "sub": "tech-9", "role": "technician", "tenant_id": TENANT,
    }
    for path in ("export.csv", "export.pdf"):
        r = tc.get(f"/api/timeclock/pay-period/{path}?start=2026-08-10&end=2026-08-23")
        assert r.status_code == 403, path


def test_a_backwards_range_is_refused(client):
    tc, _ = client
    r = tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-23&end=2026-08-10")
    assert r.status_code == 422


def test_export_is_audited(client):
    """Everyone's hours leaving the app. "Who exported that" has to have an
    answer."""
    tc, SessionLocal = client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23")

    session = SessionLocal()
    try:
        rows = session.execute(text(
            "SELECT user_id, details FROM audit_logs WHERE action = 'timesheet_exported'"
        )).fetchall()
    finally:
        session.close()
    assert rows, "an export must leave a trail"
    assert rows[0][0] == "office-1"
    assert "2026-08-10" in str(rows[0][1])


# ---------------------------------------------------------------------------
# The export when the audit trail itself fails (GDXA-44 / GDXA-58)
#
# `_audit_export` promises in its docstring that the trail is "never allowed to
# fail the download", which is `audit_best_effort`'s contract. Nothing asserted
# that promise before these tests: the suite only ever covered the happy path,
# so the swap onto the helper — and any later change to it — could regress the
# guarantee silently. A payroll export that 500s because an audit row was
# refused is the worse outcome; the bookkeeper cannot be paid by an error page.
#
# Which of these can actually fail, measured rather than assumed (GDXA-58):
# restore the old hand-rolled `try/except` and only ONE goes red,
# `..._leaves_the_session_usable_for_the_next_read`, with the exact
# `PendingRollbackError` from GDXA-44's verdict. The csv/pdf pair passes on the
# old code too, because the old swallow also returned 200 — they lock the
# contract against a future change that lets the audit raise, they do not
# detect the defect being fixed here. Said plainly so nobody reads three
# guards where there is one.
# ---------------------------------------------------------------------------

def test_a_refused_audit_row_never_fails_the_csv_download(refusing_client):
    tc, SessionLocal, _sessions = refusing_client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    r = tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "timesheet_2026-08-10_2026-08-23.csv" in r.headers["content-disposition"]
    # Not merely a 200 — the hours have to be in it. A body built from a
    # session the failed audit had poisoned is the failure being guarded.
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(CSV_HEADER)
    date_col, worked_col = CSV_HEADER.index("date"), CSV_HEADER.index("worked_hours")
    totals = [r_ for r_ in rows[1:] if r_[date_col] == "TOTAL"]
    assert len(totals) == 1, r.text
    assert totals[0][worked_col] == "9.00", r.text

    session = SessionLocal()
    try:
        landed = session.execute(text(
            "SELECT count(*) FROM audit_logs WHERE action = 'timesheet_exported'"
        )).scalar_one()
    finally:
        session.close()
    assert landed == 0, "the trigger is the point; if the row landed this proves nothing"


def test_a_refused_audit_row_never_fails_the_pdf_download(refusing_client):
    """The second call site — the 200 only, and deliberately not more.

    This one cannot observe a poisoned session even in principle: the PDF
    handler calls `build_pdf` BEFORE `_audit_export`, so the audit is the last
    statement in the request and there is nothing after it to break. It locks
    the never-fail-the-download contract for the second endpoint; the session
    guarantee is `..._leaves_the_session_usable_for_the_next_read`'s job, on
    the csv path where a read actually follows.
    """
    tc, SessionLocal, _sessions = refusing_client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    r = tc.get("/api/timeclock/pay-period/export.pdf?start=2026-08-10&end=2026-08-23")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert len(r.content) > 1000


def test_a_refused_audit_leaves_the_session_usable_for_the_next_read(
    refusing_client, monkeypatch
):
    """The class defect itself, made observable.

    At this site the old hand-rolled swallow was *latent*: both handlers stop
    touching `db` after the audit, so a deactivated session was never noticed.
    It bites the moment anyone adds a db read after the audit — so this test
    adds one, by standing in for that future edit at the real call site.

    Without the helper this is a 500: `log_audit_event_sync` ends in a flush,
    the refused INSERT deactivates the session, and the next `execute` raises
    `PendingRollbackError`. With it, the savepoint contains the failure and the
    session the handler is still holding keeps working.
    """
    tc, SessionLocal, handler_sessions = refusing_client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    observed: dict[str, object] = {}
    real_build_csv = timeclock_router_module.build_csv

    def _build_csv_after_reading_the_db(sheet):
        observed["read"] = handler_sessions[-1].execute(select(1)).scalar_one()
        return real_build_csv(sheet)

    monkeypatch.setattr(
        timeclock_router_module, "build_csv", _build_csv_after_reading_the_db
    )

    r = tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23")

    assert r.status_code == 200, r.text
    assert observed["read"] == 1, "the handler's session was not usable after the audit"
    assert "TOTAL" in r.text


def test_the_export_stages_nothing_so_the_helpers_precondition_holds(client, caplog):
    """The precondition that makes `audit_best_effort` the right helper here.

    It COMMITS, so it is only safe at a caller with nothing staged — otherwise
    a GET hardens pending work nobody asked it to. `_audit_export`'s docstring
    asserts `_export_context` is query-only; this is what checks it, because a
    documented precondition that nothing verifies is how GDXA-44's defect class
    arrived. The helper WARNs on violation, and a warning no test reads is not
    a guard.

    Goes red if a later edit stages a row anywhere on the export path —
    `_export_context`, `build_timesheet`, or a new dependency — which is
    exactly when this site must move to `audit_or_rollback`.
    """
    tc, SessionLocal = client
    _seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    with caplog.at_level(logging.WARNING, logger="gdx_dispatch.core.audit"):
        r = tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23")

    assert r.status_code == 200, r.text
    staged = [
        rec.getMessage() for rec in caplog.records
        if "audit_best_effort_caller_has_pending_work" in rec.getMessage()
    ]
    assert not staged, staged


def test_one_person_can_be_exported_alone(client):
    tc, SessionLocal = client
    _seed(SessionLocal, entry_id="e1", tech=MICHAEL, clock_in="2026-08-17T13:00:00+00:00",
          clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _seed(SessionLocal, entry_id="e2", tech=AMBER, clock_in="2026-08-18T14:00:00+00:00",
          clock_out="2026-08-18T18:00:00+00:00", minutes=240)

    r = tc.get(
        f"/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23&technician_id={AMBER}"
    )
    assert r.status_code == 200
    assert MICHAEL not in r.text
    assert AMBER in r.text
