"""GDXA-511 — the phone's "today" is the shop's calendar day, not UTC's.

The clock is frozen at 03:00 UTC on 2026-10-10, which is 22:00 on
2026-10-09 in America/Chicago: past UTC midnight, before shop midnight. Every
test here fails on the old code, which took the UTC date (and UTC day bounds)
and so moved the tech's evening onto tomorrow.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core import pay_periods
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Appointment, AppSettings, Customer, Job, Technician
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers import mobile_day_summary
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"
SHOP_TZ = "America/Chicago"
FROZEN_NOW = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)  # 2026-10-09 22:00 CDT
SHOP_TODAY = "2026-10-09"


def _utc(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


@pytest.fixture
def client_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    # Not freezegun: it swaps ``date`` inside the router module, and FastAPI
    # then cannot analyse GET /today's ``date | None`` parameter.
    real_shop_today = pay_periods.shop_today_from_settings

    def _frozen_shop_today(db, *, now=None):
        return real_shop_today(db, now=now or FROZEN_NOW)

    class _FrozenDatetime(datetime):
        # Construction still yields a real datetime, which SQLite can bind.
        def __new__(cls, *args, **kwargs):
            return datetime(*args, **kwargs)

        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)

    # raising=False: the pre-fix module has no such name, and this file must
    # still run against it (the falsifier) and fail on the assertions.
    monkeypatch.setattr(
        mobile_day_summary, "shop_today_from_settings", _frozen_shop_today,
        raising=False,
    )
    monkeypatch.setattr(mobile_router, "datetime", _FrozenDatetime)

    engine = make_fresh_db()
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    s.add(AppSettings(
        timezone=SHOP_TZ,
        tenant_mobile_settings={"tech_mobile.drag_reorder_authority": "live"},
    ))
    s.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    s.commit()

    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.include_router(mobile_day_summary.router)
    user = {"user_id": USER, "tenant_id": TENANT, "role": "technician"}
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = user
        return await call_next(request)

    yield TestClient(app), s
    s.close()
    engine.dispose()


def _customer(db) -> Customer:
    c = Customer(id=uuid4(), name="Evening Customer", phone="555-0000",
                 email="e@x.com", address="1 Main", company_id=TENANT)
    db.add(c)
    return c


def _job(db, c, *, title: str, scheduled_at: datetime | None = None,
         completed_at: datetime | None = None, assigned_to: str = TECH) -> Job:
    j = Job(id=uuid4(), company_id=TENANT, customer_id=c.id, title=title,
            description="", scheduled_at=scheduled_at, completed_at=completed_at,
            assigned_to=assigned_to, dispatch_status="assigned")
    db.add(j)
    return j


# ── /api/mobile/day-summary ───────────────────────────────────────────


def test_day_summary_default_date_is_shop_today(client_and_db):
    client, _ = client_and_db
    body = client.get("/api/mobile/day-summary").json()
    assert body["date"] == SHOP_TODAY


def test_day_summary_counts_the_shop_evening_on_its_own_day(client_and_db):
    """A one-hour entry clocked in at 20:00 CDT (01:00 UTC the next day)
    belongs to the shop's day; UTC bounds put it on tomorrow's summary."""
    client, db = client_and_db
    c = _customer(db)
    j = _job(db, c, title="Evening repair", scheduled_at=_utc(10, 0))
    db.flush()
    clock_in = _utc(10, 1)
    db.execute(
        text(
            "INSERT INTO time_entries (id, job_id, company_id, tech_id, user_id, "
            "clock_in, clock_out, duration_minutes, entry_type, created_at, updated_at) "
            "VALUES (:id, :job_id, :co, :tech, :uid, :ci, :co_out, 60, 'manual', :ci, :ci)"
        ),
        {"id": str(uuid4()), "job_id": j.id.hex, "co": TENANT, "tech": TECH,
         "uid": USER, "ci": clock_in, "co_out": _utc(10, 2)},
    )
    db.commit()

    body = client.get(f"/api/mobile/day-summary?date={SHOP_TODAY}").json()
    assert body["labor_hours"] == 1.0
    nxt = client.get("/api/mobile/day-summary?date=2026-10-10").json()
    assert nxt["labor_hours"] == 0.0


def test_tomorrows_first_stop_is_the_shop_tomorrow(client_and_db):
    """Tonight's 21:00 CDT job (02:00 UTC on the 10th) is today's work, not
    tomorrow's first stop; tomorrow's 20:00 CDT job (01:00 UTC on the 11th)
    is. DATE(scheduled_at) on the UTC day picked the wrong one."""
    client, db = client_and_db
    c = _customer(db)
    _job(db, c, title="Tonight", scheduled_at=_utc(10, 2), assigned_to=USER)
    tomorrow = _job(db, c, title="Tomorrow evening", scheduled_at=_utc(11, 1),
                    assigned_to=USER)
    db.commit()

    stop = client.get(f"/api/mobile/day-summary?date={SHOP_TODAY}").json()["next_first_stop"]
    assert stop is not None
    # SQLite hands raw-SQL Uuid back as 32 dashless hex.
    assert UUID(stop["id"]) == tomorrow.id


# ── /api/mobile/today/reorder ─────────────────────────────────────────


def _appt(db, c, start: datetime) -> Appointment:
    j = _job(db, c, title=f"Visit {start.isoformat()}", scheduled_at=start)
    a = Appointment(id=uuid4(), company_id=TENANT, job_id=j.id, customer_id=c.id,
                    tech_id=TECH, title=j.title, start_at=start,
                    end_at=start.replace(minute=45))
    db.add(a)
    return a


def test_reorder_uses_the_shop_day(client_and_db):
    """At 22:00 CDT the route is the shop's day: 15:00 and 21:00 CDT.
    Tomorrow's 09:00 CDT visit shares tonight's UTC date and is not on it."""
    client, db = client_and_db
    c = _customer(db)
    afternoon = _appt(db, c, _utc(9, 20))  # 15:00 CDT on the 9th
    evening = _appt(db, c, _utc(10, 2))    # 21:00 CDT on the 9th
    _appt(db, c, _utc(10, 14))             # 09:00 CDT on the 10th
    db.commit()

    r = client.post(
        "/api/mobile/today/reorder",
        json={"appointment_ids": [str(evening.id), str(afternoon.id)]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True


def _seed_evening_route(db):
    """15:00 and 21:00 CDT on the 9th, and 23:30 CDT on the 9th — which is
    00:30 on the 10th in New York."""
    c = _customer(db)
    afternoon = _appt(db, c, _utc(9, 20))
    evening = _appt(db, c, _utc(10, 2))
    late = _appt(db, c, datetime(2026, 10, 10, 4, 30, tzinfo=UTC))
    db.commit()
    return afternoon, evening, late


def _route_ids(client, query: str) -> list[str]:
    body = client.get(f"/api/mobile/today{query}").json()
    return [j["appointment_id"] for j in body["jobs"]]


def test_today_without_a_device_zone_is_the_shop_day(client_and_db):
    """A phone that sends no tz gets the shop's evening, not UTC's
    tomorrow; the reorder of that route is accepted. The old fallback was
    UTC: the 10th, where only the 23:30 CDT visit lives."""
    client, db = client_and_db
    afternoon, evening, late = _seed_evening_route(db)

    ids = _route_ids(client, "")
    assert set(ids) == {str(afternoon.id), str(evening.id), str(late.id)}

    r = client.post("/api/mobile/today/reorder",
                    json={"appointment_ids": list(reversed(ids))})
    assert r.status_code == 200, r.text


def test_reorder_reads_the_day_get_today_read(client_and_db):
    """A phone in New York (shop in Chicago) sees the 9th without the 00:30
    EDT visit; its reorder, sent with the same tz, is that route, not the
    shop's three-stop one."""
    client, db = client_and_db
    afternoon, evening, _late = _seed_evening_route(db)

    ids = _route_ids(client, "?date=2026-10-09&tz=America/New_York")
    assert set(ids) == {str(afternoon.id), str(evening.id)}

    r = client.post(
        "/api/mobile/today/reorder",
        json={"appointment_ids": list(reversed(ids)), "tz": "America/New_York"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True


def test_an_invalid_device_zone_falls_back_to_the_shop_day(client_and_db):
    """A tz the server cannot resolve degrades to the shop's day, as a
    missing one does; the old fallback was UTC's tomorrow."""
    client, db = client_and_db
    afternoon, evening, late = _seed_evening_route(db)

    ids = _route_ids(client, "?tz=Not/AZone")
    assert set(ids) == {str(afternoon.id), str(evening.id), str(late.id)}


def test_reorder_of_a_route_with_a_cancelled_visit(client_and_db):
    """GET /today leaves a cancelled visit off the route, so the reorder of
    what the tech sees must not demand it (audit, GDXA-511)."""
    client, db = client_and_db
    afternoon, evening, _late = _seed_evening_route(db)
    cancelled = _appt(db, _customer(db), _utc(9, 22))  # 17:00 CDT on the 9th
    cancelled.status = "cancelled"
    db.flush()
    # The visit was cancelled and its job moved to Monday, so nothing of it
    # is on tonight's route.
    db.get(Job, cancelled.job_id).scheduled_at = _utc(12, 14)
    db.commit()

    ids = _route_ids(client, "?date=2026-10-09&tz=America/New_York")
    assert str(cancelled.id) not in ids

    r = client.post(
        "/api/mobile/today/reorder",
        json={"appointment_ids": list(reversed(ids)), "tz": "America/New_York"},
    )
    assert r.status_code == 200, r.text
