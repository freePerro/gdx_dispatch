"""Tests for the scheduling router (calendar views + unavailability + conflicts)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.appointments import Appointment
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.scheduling import router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
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
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._session_cls = Session  # type: ignore[attr-defined]
    tc._tenant_id = tenant_id  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _insert_appt(client, *, tech_id, start, end, status="scheduled", tenant_id=None):
    Session = client._session_cls  # type: ignore[attr-defined]
    db = Session()
    try:
        row = Appointment(
            company_id=tenant_id or client._tenant_id,
            tech_id=tech_id,
            title="Test appt",
            start_at=start,
            end_at=end,
            status=status,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Calendar views
# ---------------------------------------------------------------------------


def test_month_view_counts_appointments(client):
    # Insert 3 appts on different days in April 2026
    for day in (3, 10, 20):
        start = datetime(2026, 4, day, 10, 0, tzinfo=timezone.utc)
        _insert_appt(client, tech_id="t1", start=start, end=start + timedelta(hours=1))

    r = client.get("/api/calendar/month", params={"year": 2026, "month": 4})
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 30  # April has 30 days
    by_date = {d["date"]: d for d in data}
    assert by_date["2026-04-03"]["appointment_count"] == 1
    assert by_date["2026-04-10"]["appointment_count"] == 1
    assert by_date["2026-04-20"]["appointment_count"] == 1
    assert by_date["2026-04-05"]["appointment_count"] == 0


def test_week_view_returns_7_days(client):
    r = client.get("/api/calendar/week", params={"date": "2026-04-08"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 7


def test_events_date_range(client):
    in_range = datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc)
    out_of_range = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    _insert_appt(client, tech_id="t1", start=in_range, end=in_range + timedelta(hours=1))
    _insert_appt(client, tech_id="t1", start=out_of_range, end=out_of_range + timedelta(hours=1))

    r = client.get(
        "/api/calendar/events",
        params={"start": "2026-04-01T00:00:00Z", "end": "2026-04-30T23:59:59Z"},
    )
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_events_tech_filter(client):
    start = datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc)
    _insert_appt(client, tech_id="tech-A", start=start, end=start + timedelta(hours=1))
    _insert_appt(client, tech_id="tech-B", start=start, end=start + timedelta(hours=1))

    r = client.get(
        "/api/calendar/events",
        params={
            "start": "2026-04-01T00:00:00Z",
            "end": "2026-04-30T00:00:00Z",
            "tech_id": "tech-A",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["tech_id"] == "tech-A"


# ---------------------------------------------------------------------------
# Availability / conflicts
# ---------------------------------------------------------------------------


def test_available_slots_excludes_booked_hours(client):
    # Book 10:00–11:00 UTC for tech-1 on a fixed date
    d = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
    _insert_appt(client, tech_id="tech-1", start=d, end=d + timedelta(hours=1))

    r = client.get(
        "/api/appointments/available-slots",
        params={"tech_id": "tech-1", "date": "2026-04-15", "duration_minutes": 60},
    )
    assert r.status_code == 200
    slots = r.json()
    starts = {s["start_at"] for s in slots}
    # 10:00 should be excluded
    assert not any("T10:00" in s for s in starts)
    # 09:00 and 11:00 should exist
    assert any("T09:00" in s for s in starts)
    assert any("T11:00" in s for s in starts)


def test_available_slots_excludes_unavailability(client):
    # Tech is unavailable 14:00–16:00 on 2026-04-16
    r = client.post(
        "/api/tech-unavailability",
        json={
            "tech_id": "tech-2",
            "start_at": "2026-04-16T14:00:00+00:00",
            "end_at": "2026-04-16T16:00:00+00:00",
            "reason": "doctor appointment",
        },
    )
    assert r.status_code == 201, r.text

    r = client.get(
        "/api/appointments/available-slots",
        params={"tech_id": "tech-2", "date": "2026-04-16", "duration_minutes": 60},
    )
    assert r.status_code == 200
    starts = {s["start_at"] for s in r.json()}
    assert not any("T14:00" in s for s in starts)
    assert not any("T15:00" in s for s in starts)
    assert any("T13:00" in s for s in starts)
    assert any("T16:00" in s for s in starts)


def test_conflicts_detects_overlap(client):
    d = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)
    _insert_appt(client, tech_id="tech-x", start=d, end=d + timedelta(hours=2))  # 10:00–12:00
    _insert_appt(client, tech_id="tech-x", start=d + timedelta(hours=1), end=d + timedelta(hours=3))  # 11:00–13:00

    r = client.get("/api/schedule/conflicts", params={"date": "2026-04-17"})
    assert r.status_code == 200
    conflicts = r.json()
    assert len(conflicts) >= 1
    assert conflicts[0]["tech_id"] == "tech-x"


# ---------------------------------------------------------------------------
# Tech unavailability CRUD
# ---------------------------------------------------------------------------


def test_tech_unavailability_crud(client):
    r = client.post(
        "/api/tech-unavailability",
        json={
            "tech_id": "tech-7",
            "start_at": "2026-05-01T00:00:00+00:00",
            "end_at": "2026-05-08T00:00:00+00:00",
            "reason": "vacation",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["tech_id"] == "tech-7"
    assert created["reason"] == "vacation"

    # List
    r = client.get("/api/tech-unavailability", params={"tech_id": "tech-7"})
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Delete
    r = client.delete(f"/api/tech-unavailability/{created['id']}")
    assert r.status_code == 204

    # Should no longer appear
    r = client.get("/api/tech-unavailability", params={"tech_id": "tech-7"})
    assert r.status_code == 200
    assert len(r.json()) == 0


def test_reject_unavail_end_before_start(client):
    r = client.post(
        "/api/tech-unavailability",
        json={
            "tech_id": "tech-8",
            "start_at": "2026-05-10T12:00:00+00:00",
            "end_at": "2026-05-10T10:00:00+00:00",  # before start
            "reason": "bad data",
        },
    )
    assert r.status_code == 422


def test_bad_date_param_returns_422(client):
    r = client.get("/api/calendar/month", params={"year": 2026, "month": 13})
    assert r.status_code == 422

    r = client.get("/api/calendar/week", params={"date": "not-a-date"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Recurring schedule expansion
# ---------------------------------------------------------------------------


def test_recurring_expand_missing_schedule_returns_zero(client):
    # No recurring_job_schedules table exists in the test DB → graceful 0 expanded
    r = client.post(f"/api/recurring-schedules/{uuid4()}/generate", params={"horizon_days": 30})
    assert r.status_code == 200
    body = r.json()
    assert body.get("expanded") == 0
