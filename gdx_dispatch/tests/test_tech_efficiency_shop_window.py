"""Tech efficiency's daily and weekly windows are the shop's days (GDXA-510).

Both windows were cut on the UTC calendar: at 23:30 Central the "daily"
leaderboard had already rolled to tomorrow, and showed nothing closed out
tonight. The windows are now the shop's midnights, as UTC instants.

The query itself is Postgres-only (DISTINCT ON), so it is replaced by a
recorder; what is under test is the window handed to it.
"""
from __future__ import annotations

from datetime import UTC, datetime

import freezegun
import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import tech_efficiency as te

freezegun.configure(extend_ignore_list=["_pytest"])


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tech_eff.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(AppSettings(timezone="America/Chicago"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _windows(db, monkeypatch, now: str) -> list[tuple[datetime, datetime]]:
    seen: list[tuple[datetime, datetime]] = []

    def _record(_db, start, end):
        seen.append((start, end))
        return []

    monkeypatch.setattr(te, "_query_efficiency", _record)
    with freeze_time(now):
        te.tech_efficiency(
            request=Request({"type": "http", "method": "GET", "path": "/", "headers": []}),
            db=db,
        )
    return seen


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_daily_window_is_the_shop_day_at_2330_central(db, monkeypatch):
    # 04:30 UTC on 10-10 is 23:30 CDT on Friday 2026-10-09.
    daily, _ = _windows(db, monkeypatch, "2026-10-10 04:30:00")
    assert daily == (_utc(2026, 10, 9, 5), _utc(2026, 10, 10, 5))


def test_weekly_window_is_the_shop_iso_week(db, monkeypatch):
    # Sunday 2026-10-11 20:00 CDT is Monday 01:00 UTC: still the week of
    # Monday 2026-10-05 in the shop, already next week on the UTC calendar.
    _, weekly = _windows(db, monkeypatch, "2026-10-12 01:00:00")
    assert weekly == (_utc(2026, 10, 5, 5), _utc(2026, 10, 12, 5))


def test_weekly_window_ends_at_shop_midnight_across_a_dst_change(db, monkeypatch):
    # Week of Mon 2026-10-26 (CDT, UTC-5) to Mon 2026-11-02 (CST, UTC-6).
    _, weekly = _windows(db, monkeypatch, "2026-10-30 18:00:00")
    assert weekly == (_utc(2026, 10, 26, 5), _utc(2026, 11, 2, 6))


def test_payroll_default_range_is_the_shop_month_to_date(db, monkeypatch):
    """GDXA-510 sibling: /api/payroll/summary with no dates is month to date.
    At 22:00 CDT on Oct 31 the UTC clock already read Nov 1, and the default
    range was the single day 2026-11-01."""
    from gdx_dispatch.routers import payroll as pr

    seen = {}

    def _record(_db, *, tenant_id, start, end):
        seen["range"] = (start, end)
        return []

    monkeypatch.setattr(pr, "_build_summary_rows", _record)
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    with freeze_time("2026-11-01 03:00:00"):
        pr.payroll_summary(request=req, _={}, db=db, start=None, end=None)
    assert [d.isoformat() for d in seen["range"]] == ["2026-10-01", "2026-10-31"]


def test_payroll_hours_land_on_the_shop_day_they_were_worked(db):
    """GDXA-510 sibling: `_fetch_tech_hours` grouped by `DATE(clock_in)`, the
    UTC day. A 10pm CDT shift on Oct 30 is stamped Oct 31 UTC, so a range
    ending Oct 30 paid it nothing and an Oct 31 day showed an hour never
    worked that day."""
    from datetime import date

    from gdx_dispatch.models.tenant_models import TimeEntry
    from gdx_dispatch.routers.payroll import _fetch_tech_hours

    for clock_in, minutes in (
        (_utc(2026, 10, 30, 15), 120),  # 10:00 CDT Oct 30
        (_utc(2026, 10, 31, 3), 60),    # 22:00 CDT Oct 30, Oct 31 on the UTC clock
    ):
        db.add(TimeEntry(
            company_id="tenant-a", tech_id="tech-1", user_id="tech-1",
            clock_in=clock_in, clock_out=clock_in, duration_minutes=minutes,
        ))
    db.commit()

    hours = _fetch_tech_hours(
        db, tenant_id="tenant-a", start=date(2026, 10, 30), end=date(2026, 10, 30)
    )
    assert hours == {"tech-1": {date(2026, 10, 30): 3.0}}
