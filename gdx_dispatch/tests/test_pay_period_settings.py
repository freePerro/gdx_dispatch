"""The pay-period settings surface, and the calendar endpoint it previews.

Two failure shapes are pinned here, both of the "succeeds while doing
nothing" family this repo treats as the highest class of defect:

1. A row saved into a state that cannot produce a period. Setting cadence
   to biweekly without an anchor is accepted by naive per-field validation
   — each field is individually fine — and the surface that discovers the
   problem is then a Celery task at 7am on a Monday, not the screen the
   operator is looking at.

2. Automatic sending switched ON with no recipient. The task runs, finds
   nowhere to send, and does nothing forever while Settings reads "on".

The endpoint tests exist because the Settings preview and the Timesheets
presets must not compute periods themselves. If this endpoint is wrong,
both are wrong the same way — which is the point.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import settings as settings_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.settings import SettingsPatchIn
from gdx_dispatch.routers.timeclock import router as timeclock_router

TENANT = "tenant-test"


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL, module_key TEXT NOT NULL,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))
    """))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _admin() -> dict[str, str]:
    return {"user_id": "admin-1", "sub": "admin-1", "tenant_id": TENANT, "role": "admin"}


def _request() -> Request:
    request = Request({"type": "http", "headers": [], "client": None})
    request.state.tenant = {"id": TENANT, "slug": TENANT}
    return request


def _patch(db: Session, **fields):
    return settings_router.patch_settings(
        payload=SettingsPatchIn(**fields),
        request=_request(),
        current_user=_admin(),
        db=db,
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_defaults_are_neutral_not_this_shops_biweekly(db_session: Session):
    """The column default ships to every install. weekly_mon is what the
    timesheet screens already did, so an upgrade changes nobody's view."""
    data = settings_router.get_settings(current_user=_admin(), db=db_session)
    assert data["pay_period_cadence"] == "weekly_mon"
    assert data["pay_period_anchor_start"] == ""
    assert data["pay_period_pay_lag_days"] == 0
    assert data["payroll_recipient_emails"] == ""
    assert data["payroll_autosend_enabled"] is False


# ---------------------------------------------------------------------------
# The configuration that cannot produce a period
# ---------------------------------------------------------------------------

def test_biweekly_without_an_anchor_is_refused(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        _patch(db_session, pay_period_cadence="biweekly")
    assert exc.value.status_code == 422
    assert "start date" in str(exc.value.detail)


def test_biweekly_with_an_anchor_saves(db_session: Session):
    saved = _patch(
        db_session,
        pay_period_cadence="biweekly",
        pay_period_anchor_start=date(2026, 8, 10),
        pay_period_pay_lag_days=5,
    )
    assert saved["pay_period_cadence"] == "biweekly"
    assert saved["pay_period_anchor_start"] == "2026-08-10"
    assert saved["pay_period_pay_lag_days"] == 5


def test_the_anchor_can_be_set_in_a_later_request(db_session: Session):
    """Cross-field validation runs against the MERGED row, not the payload.

    Judging the payload alone would reject this legitimate edit, since it
    carries no cadence at all.
    """
    _patch(
        db_session,
        pay_period_cadence="biweekly",
        pay_period_anchor_start=date(2026, 8, 10),
    )
    saved = _patch(db_session, pay_period_anchor_start=date(2026, 8, 24))
    assert saved["pay_period_anchor_start"] == "2026-08-24"
    assert saved["pay_period_cadence"] == "biweekly"


def test_clearing_the_anchor_of_a_biweekly_row_is_refused(db_session: Session):
    """The row would be left in a state where the period math raises."""
    _patch(
        db_session,
        pay_period_cadence="biweekly",
        pay_period_anchor_start=date(2026, 8, 10),
    )
    with pytest.raises(HTTPException) as exc:
        _patch(db_session, pay_period_anchor_start=None)
    assert exc.value.status_code == 422


def test_weekly_needs_no_anchor(db_session: Session):
    saved = _patch(db_session, pay_period_cadence="weekly_sun")
    assert saved["pay_period_cadence"] == "weekly_sun"
    assert saved["pay_period_anchor_start"] == ""


def test_an_unknown_cadence_is_refused(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        _patch(db_session, pay_period_cadence="fortnightly-ish")
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Sending with nowhere to send
# ---------------------------------------------------------------------------

def test_autosend_without_a_recipient_is_refused(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        _patch(db_session, payroll_autosend_enabled=True)
    assert exc.value.status_code == 422


def test_autosend_with_a_recipient_saves(db_session: Session):
    saved = _patch(
        db_session,
        payroll_recipient_emails="bookkeeper@example.com",
        payroll_autosend_enabled=True,
        payroll_autosend_hour=7,
    )
    assert saved["payroll_autosend_enabled"] is True
    assert saved["payroll_autosend_hour"] == 7


def test_removing_the_recipient_while_autosend_is_on_is_refused(db_session: Session):
    _patch(
        db_session,
        payroll_recipient_emails="bookkeeper@example.com",
        payroll_autosend_enabled=True,
    )
    with pytest.raises(HTTPException) as exc:
        _patch(db_session, payroll_recipient_emails="")
    assert exc.value.status_code == 422


def test_a_mistyped_address_is_caught_at_the_screen(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        _patch(db_session, payroll_recipient_emails="bookkeeper@example, ok@x.com")
    assert exc.value.status_code == 422
    assert "bookkeeper@example" in str(exc.value.detail)


def test_several_recipients_are_allowed(db_session: Session):
    saved = _patch(
        db_session, payroll_recipient_emails="a@example.com, b@example.com"
    )
    assert saved["payroll_recipient_emails"] == "a@example.com, b@example.com"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_saving_a_date_does_not_break_the_audit_row(db_session: Session):
    """The audit dump is mode='json' precisely so a date serializes. A plain
    dump puts a datetime.date into the details column and raises downstream."""
    _patch(
        db_session,
        pay_period_cadence="biweekly",
        pay_period_anchor_start=date(2026, 8, 10),
    )
    rows = db_session.execute(
        text("SELECT details FROM audit_logs WHERE action = 'settings_updated'")
    ).fetchall()
    assert rows, "settings_updated must be audited"
    assert any("2026-08-10" in str(r[0]) for r in rows)


# ---------------------------------------------------------------------------
# The calendar endpoint both surfaces read
# ---------------------------------------------------------------------------

@pytest.fixture()
def clock_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = SessionLocal()
    for ddl in (
        """CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))""",
    ):
        setup.execute(text(ddl))
    setup.execute(text(
        "INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)"
        " VALUES ('g1', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)"
        " VALUES ('g2', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.commit()
    setup.close()

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

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


def _configure(SessionLocal, **kw):
    db = SessionLocal()
    try:
        row = db.query(AppSettings).first()
        if row is None:
            row = AppSettings(
                company_name="", address="", phone="", email="", logo="",
                timezone=kw.pop("timezone", "America/Chicago"),
                enabled_modules=[], notification_preferences={}, integrations={},
            )
            db.add(row)
        for key, value in kw.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def test_calendar_endpoint_reports_dougs_periods(clock_client):
    client, SessionLocal = clock_client
    _configure(
        SessionLocal,
        pay_period_cadence="biweekly",
        pay_period_anchor_start=date(2026, 8, 10),
        pay_period_pay_lag_days=5,
    )
    body = client.get("/api/timeclock/pay-periods").json()
    assert body["configured"] is True
    assert body["cadence"] == "biweekly"
    assert body["timezone"] == "America/Chicago"
    # Periods abut, and the pay date sits `lag` days past the end.
    assert body["previous"]["end"] < body["current"]["start"]
    assert body["current"]["end"] < body["next"]["start"]
    prev_end = date.fromisoformat(body["previous"]["end"])
    assert date.fromisoformat(body["previous"]["pay_date"]) == prev_end + timedelta(days=5)


def test_calendar_endpoint_says_unconfigured_instead_of_500(clock_client):
    """A settings screen about to configure this must not be greeted by an
    error page. 200 with configured=false, and a sentence saying what to do."""
    client, SessionLocal = clock_client
    _configure(SessionLocal, pay_period_cadence="biweekly", pay_period_anchor_start=None)
    response = client.get("/api/timeclock/pay-periods")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["current"] is None
    assert body["message"]


def test_calendar_endpoint_works_before_any_settings_row_exists(clock_client):
    """A fresh install has no app_settings row at all."""
    client, _ = clock_client
    body = client.get("/api/timeclock/pay-periods").json()
    assert body["configured"] is True
    assert body["cadence"] == "weekly_mon"


def test_calendar_endpoint_is_not_restricted_to_the_office(clock_client):
    """It returns date ranges only — no hours, no names, no money — and a
    tech reading their own week needs the same boundaries."""
    client, SessionLocal = clock_client
    _configure(SessionLocal, pay_period_cadence="weekly_mon")
    client.app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "tech-9", "sub": "tech-9", "role": "technician", "tenant_id": TENANT,
    }
    response = client.get("/api/timeclock/pay-periods")
    assert response.status_code == 200
    body = response.json()
    assert "hours" not in body
    assert body["current"]["start"]
