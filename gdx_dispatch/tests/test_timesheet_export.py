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
from collections.abc import Generator
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
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

@pytest.fixture()
def client():
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

    def _override_db():
        session = SessionLocal()
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
    yield TestClient(app, raise_server_exceptions=True), SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


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
