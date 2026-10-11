"""GDXA-509: warranties and labor read the shop's calendar day, not UTC's.

The clock is frozen at 02:00 UTC on 2026-03-31, which is 21:00 CDT on
2026-03-30 in the shop. Between UTC midnight and shop midnight the server's
``date.today()`` is already tomorrow; every assertion here fails on the code
that used it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.tenant_models import AppSettings, TimeEntry, Warranty
from gdx_dispatch.routers.labor import labor_summary
from gdx_dispatch.routers.warranties import expiring_warranties
from gdx_dispatch.tests.conftest import make_fresh_db

EVENING_UTC = "2026-03-31 02:00:00"
SHOP_DAY = date(2026, 3, 30)


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(AppSettings(timezone="America/Chicago"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _warranty(db, *, end: date) -> str:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    w = Warranty(id=str(uuid4()), job_id=str(uuid4()), customer_id=str(uuid4()), description="Labor",
                 start_date=date(2026, 1, 1), end_date=end, status="active", claim_count=0,
                 created_at=now, updated_at=now)
    db.add(w)
    db.commit()
    return w.id


def _entry(db, *, tech: str, clock_in: datetime, minutes: int = 60) -> None:
    db.add(TimeEntry(tech_id=tech, clock_in=clock_in, duration_minutes=minutes, hourly_rate=50,
                     entry_type="manual", company_id="tenant-509"))
    db.commit()


def test_expiring_warranties_counts_one_ending_on_the_shop_day(db):
    ends_today = _warranty(db, end=SHOP_DAY)

    with freeze_time(EVENING_UTC):
        body = expiring_warranties(_={}, db=db)

    # UTC's today is 03-31, which would drop a warranty that ends today.
    assert [row["id"] for row in body["data"]] == [ends_today]


def test_labor_summary_default_window_ends_on_the_shop_day(db):
    with freeze_time(EVENING_UTC):
        body = labor_summary(start_date=None, end_date=None, current_user={}, db=db)

    assert body["end_date"] == SHOP_DAY.isoformat()
    assert body["start_date"] == "2026-03-01"


def test_labor_summary_buckets_shifts_by_shop_day(db):
    # 20:00 CDT on 03-30: the shop's 03-30, UTC's 03-31.
    _entry(db, tech="evening", clock_in=datetime(2026, 3, 31, 1, 0, tzinfo=UTC))
    # 22:00 CDT on 03-29: the shop's 03-29, UTC's 03-30.
    _entry(db, tech="night-before", clock_in=datetime(2026, 3, 30, 3, 0, tzinfo=UTC))

    with freeze_time(EVENING_UTC):
        body = labor_summary(start_date=SHOP_DAY, end_date=SHOP_DAY, current_user={}, db=db)

    assert [row["tech_id"] for row in body["items"]] == ["evening"]
    assert body["total_hours"] == 1.0
