"""Sprint tech_mobile S1-A1 + S1-A7 — GET /api/mobile/today.

Covers the rich today's-route payload: ORM-decoded customer fields,
priority + service_type, customer notes/tags, derived alerts list. All
tests use a real in-memory tenant DB (no mocks for the data layer) so
the SQL paths execute end-to-end. Auth + tenant context are stubbed via
FastAPI dependency_overrides + a tiny middleware that stamps
request.state.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    Tag,
    TagAssignment,
    Technician,
)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db


TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _now() -> datetime:
    # Anchor at 12:00 UTC of *today* so that ``_now() + timedelta(hours=2)``
    # stays on the same UTC date. The today endpoint filters appointments
    # by ``func.date(start_at) == target_date``; a wall-clock _now() near
    # the day boundary (e.g. 22:50 UTC) made "+2h" appointments cross
    # midnight and silently drop, producing a time-of-day flake. The hour
    # value doesn't matter for the assertions — only that arithmetic
    # within the test stays inside one UTC day.
    today = datetime.now(UTC).date()
    return datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    # Bypass the require_module("mobile") gate.
    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER,
        "tenant_id": TENANT,
        "role": "technician",
    }
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = {"user_id": USER, "tenant_id": TENANT}
        return await call_next(request)

    client = TestClient(app)
    yield client, db
    db.close()
    engine.dispose()


def _seed_tech(db) -> None:
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    db.commit()


def _seed_customer(db, *, name="Acme", address="123 Main", notes=None) -> Customer:
    c = Customer(
        id=uuid4(),
        name=name,
        phone="555-1111",
        email="a@example.com",
        address=address,
        notes=notes,
        company_id=TENANT,
    )
    db.add(c)
    db.commit()
    return c


def _seed_job(
    db,
    *,
    customer_id: UUID,
    title="Spring replacement",
    job_type="Service",
    priority="Normal",
    scheduled_at: datetime | None = None,
    assigned_to: str | None = TECH,
) -> Job:
    j = Job(
        id=uuid4(),
        company_id=TENANT,
        customer_id=customer_id,
        title=title,
        description="desc",
        job_type=job_type,
        priority=priority,
        scheduled_at=scheduled_at or _now(),
        assigned_to=assigned_to,
        dispatch_status="assigned",
    )
    db.add(j)
    db.commit()
    return j


def _seed_appointment(
    db,
    *,
    job_id: UUID,
    customer_id: UUID,
    start: datetime,
    end: datetime | None = None,
) -> Appointment:
    a = Appointment(
        id=uuid4(),
        company_id=TENANT,
        job_id=job_id,
        customer_id=customer_id,
        tech_id=TECH,
        title="Today",
        start_at=start,
        end_at=end or (start + timedelta(hours=1)),
    )
    db.add(a)
    db.commit()
    return a


def _seed_tag(db, name: str, color: str = "#ff0000") -> Tag:
    t = Tag(id=uuid4(), company_id=TENANT, name=name, color=color)
    db.add(t)
    db.commit()
    return t


def _assign_tag(db, tag_id: UUID, customer_id: UUID) -> None:
    db.add(
        TagAssignment(
            id=uuid4(),
            company_id=TENANT,
            tag_id=tag_id,
            entity_type="customer",
            entity_id=str(customer_id),
        )
    )
    db.commit()


# ── Empty cases ───────────────────────────────────────────────────────


class TestEmpty:
    def test_no_technician_record_returns_empty(self, app_and_db):
        client, _ = app_and_db
        r = client.get("/api/mobile/today")
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"] == []
        assert body["count"] == 0
        assert body["tech_id"] is None

    def test_tech_with_no_jobs_returns_empty_list(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        r = client.get("/api/mobile/today")
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"] == []
        assert body["tech_id"] == TECH


# ── Appointment-driven path ───────────────────────────────────────────


class TestWithAppointments:
    def test_returns_one_card_per_appointment(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db, name="Acme One", address="11 First St")
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())

        r = client.get("/api/mobile/today")
        body = r.json()
        assert body["count"] == 1
        card = body["jobs"][0]
        assert card["customer"]["name"] == "Acme One"
        assert card["customer"]["address"] == "11 First St"
        assert card["service_type"] == "Service"
        assert card["priority"] == "Normal"
        assert card["dispatch_status"] == "assigned"
        assert card["alerts"] == []
        assert card["navigation_link"].startswith("https://maps.google.com/")

    def test_orders_by_appointment_start(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j1 = _seed_job(db, customer_id=c.id, title="Later")
        j2 = _seed_job(db, customer_id=c.id, title="Earlier")
        now = _now()
        _seed_appointment(db, job_id=j1.id, customer_id=c.id, start=now + timedelta(hours=2))
        _seed_appointment(db, job_id=j2.id, customer_id=c.id, start=now + timedelta(hours=1))
        r = client.get("/api/mobile/today")
        titles = [card["title"] for card in r.json()["jobs"]]
        assert titles == ["Earlier", "Later"]

    def test_returns_priority_and_service_type(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id, job_type="Install", priority="Urgent")
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["service_type"] == "Install"
        assert card["priority"] == "Urgent"

    def test_location_passes_through_when_appointment_geocoded(self, app_and_db):
        # S1-A5 — map view consumes job.location.{lat,lng}; stops with
        # geocoded appointments must flow them through unchanged.
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id)
        a = _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        a.lat = 46.8738
        a.lng = -96.7678
        db.commit()
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] == {"lat": 46.8738, "lng": -96.7678}

    def test_location_null_when_appointment_not_geocoded(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] is None


# ── Customer alerts (S1-A7) ───────────────────────────────────────────


class TestAlerts:
    def test_customer_tags_become_alerts(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        tag_dog = _seed_tag(db, "dog_warning", color="#ff0000")
        tag_gate = _seed_tag(db, "gate_code")
        _assign_tag(db, tag_dog.id, c.id)
        _assign_tag(db, tag_gate.id, c.id)
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())

        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["alerts"] == ["dog_warning", "gate_code"]
        # Tag color round-trips so the UI can render the tag chip in the
        # tenant's chosen color.
        names = {t["name"]: t["color"] for t in card["customer"]["tags"]}
        assert names["dog_warning"] == "#ff0000"

    def test_customer_notes_round_trip(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db, notes="Park behind the truck")
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["customer"]["notes"] == "Park behind the truck"

    def test_tags_for_other_entity_types_not_surfaced(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        tag = _seed_tag(db, "should_not_show")
        # Assign to a job, not a customer — the alerts feed must only
        # surface customer-scoped tags.
        db.add(
            TagAssignment(
                id=uuid4(),
                company_id=TENANT,
                tag_id=tag.id,
                entity_type="job",
                entity_id=str(uuid4()),
            )
        )
        db.commit()
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["alerts"] == []

    def test_other_tenant_tags_isolated(self, app_and_db):
        # Defensive: tag with a different company_id must never bleed into
        # this tenant's alerts. The tenant-plane is by-connection isolated
        # so this can't happen in production, but a buggy company_id on a
        # TagAssignment would shortcut the filter — pin it.
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        cross_tag = Tag(
            id=uuid4(), company_id="other-tenant", name="cross_tenant", color="#000"
        )
        db.add(cross_tag)
        db.commit()
        db.add(
            TagAssignment(
                id=uuid4(),
                company_id="other-tenant",
                tag_id=cross_tag.id,
                entity_type="customer",
                entity_id=str(c.id),
            )
        )
        db.commit()
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert "cross_tenant" not in card["alerts"]


# ── Fallback (no appointment row) ─────────────────────────────────────


class TestFallback:
    def test_jobs_with_scheduled_at_no_appointment(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db, name="QB Imported", address="7 Lane")
        _seed_job(db, customer_id=c.id, scheduled_at=_now())
        # Note: no Appointment row.
        body = client.get("/api/mobile/today").json()
        assert body["count"] == 1
        assert body["jobs"][0]["customer"]["name"] == "QB Imported"
        assert body["jobs"][0]["appointment_id"] is None

    def test_jobs_assigned_to_other_tech_filtered_out(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        _seed_job(db, customer_id=c.id, scheduled_at=_now(), assigned_to="other-tech")
        body = client.get("/api/mobile/today").json()
        assert body["count"] == 0

    def test_yesterday_jobs_not_returned(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        _seed_job(db, customer_id=c.id, scheduled_at=_now() - timedelta(days=2))
        body = client.get("/api/mobile/today").json()
        assert body["count"] == 0


# ── Date override ─────────────────────────────────────────────────────


class TestDateOverride:
    def test_explicit_date_returns_jobs_for_that_date(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db, name="Tomorrow Customer")
        tomorrow = (_now() + timedelta(days=1)).date()
        # Schedule the appointment for tomorrow.
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(
            db,
            job_id=j.id,
            customer_id=c.id,
            start=datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, tzinfo=UTC),
        )
        body = client.get(f"/api/mobile/today?date={tomorrow.isoformat()}").json()
        assert body["count"] == 1
        assert body["date"] == tomorrow.isoformat()


# ── Phase 1.3 C2 — parts_summary on each card ─────────────────────────


class TestPartsSummary:
    def test_card_has_zero_summary_when_no_parts(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["parts_summary"] == {
            "total": 0, "needed": 0, "ordered": 0, "received": 0,
        }

    def test_card_counts_parts_by_status(self, app_and_db):
        from datetime import datetime, timezone
        from sqlalchemy import text as _sql_text
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        # Two needed, one ordered, one received.
        for st, q in (("needed", 1), ("needed", 2), ("ordered", 1), ("received", 1)):
            db.execute(_sql_text(
                "INSERT INTO job_parts_needed (id, company_id, job_id, part_name, "
                "quantity, status, urgency, created_at, updated_at) "
                "VALUES (:i, :t, :j, 'spring', :q, :st, 'normal', :n, :n)"
            ), {
                "i": uuid4().hex, "t": TENANT, "j": str(j.id),
                "q": q, "st": st, "n": datetime.now(timezone.utc),
            })
        db.commit()
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["parts_summary"] == {
            "total": 4, "needed": 2, "ordered": 1, "received": 1,
        }


# ── 2026-07-16 tech-mobile job-access fix: union + area jobs + local tz ──


def _seed_assignment(db, *, job_id, tech_id=TECH) -> None:
    from gdx_dispatch.models.tenant_models import JobAssignment

    db.add(JobAssignment(id=uuid4().hex, job_id=str(job_id), tech_id=tech_id))
    db.commit()


class TestScheduledUnion:
    def test_scheduled_job_shows_alongside_appointments(self, app_and_db):
        """Pre-fix: ONE appointment hid every scheduled-but-appointmentless
        job (the fallback only ran when the appointment list was empty)."""
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j_appt = _seed_job(db, customer_id=c.id, title="With appointment")
        _seed_appointment(db, job_id=j_appt.id, customer_id=c.id, start=_now())
        j_sched = _seed_job(
            db, customer_id=c.id, title="Scheduled only",
            scheduled_at=_now() + timedelta(hours=2),
        )
        body = client.get("/api/mobile/today").json()
        titles = [card["title"] for card in body["jobs"]]
        assert titles == ["With appointment", "Scheduled only"]
        assert str(j_sched.id) in {card["id"] for card in body["jobs"]}

    def test_no_duplicate_when_job_has_appointment(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        body = client.get("/api/mobile/today").json()
        assert body["count"] == 1

    def test_scheduled_job_via_job_assignments_row(self, app_and_db):
        """Multi-tech jobs link through job_assignments, not assigned_to —
        the scheduled-jobs union must match them too."""
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id, assigned_to=None)
        _seed_assignment(db, job_id=j.id)
        body = client.get("/api/mobile/today").json()
        assert body["count"] == 1
        assert body["jobs"][0]["id"] == str(j.id)


class TestAreaJobs:
    def test_undated_assigned_job_lands_in_area_jobs(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = Job(
            id=uuid4(), company_id=TENANT, customer_id=c.id,
            title="When in the area", description="d",
            scheduled_at=None, assigned_to=TECH,
            dispatch_status="assigned", lifecycle_stage="scheduled",
        )
        db.add(j)
        db.commit()
        body = client.get("/api/mobile/today").json()
        assert body["count"] == 0
        assert body["area_count"] == 1
        assert body["area_jobs"][0]["id"] == str(j.id)
        assert body["area_jobs"][0]["customer"]["name"] == "Acme"

    def test_done_and_terminal_stages_excluded(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        for stage, status in (
            ("completed", "assigned"),
            ("cancelled", "assigned"),
            ("lead", "assigned"),
            ("estimate", "assigned"),
            ("scheduled", "done"),
        ):
            db.add(Job(
                id=uuid4(), company_id=TENANT, customer_id=c.id,
                title=f"{stage}/{status}", description="d",
                scheduled_at=None, assigned_to=TECH,
                dispatch_status=status, lifecycle_stage=stage,
            ))
        db.commit()
        body = client.get("/api/mobile/today").json()
        assert body["area_count"] == 0

    def test_area_jobs_via_job_assignments_row(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = Job(
            id=uuid4(), company_id=TENANT, customer_id=c.id,
            title="Area via assignment", description="d",
            scheduled_at=None, assigned_to=None,
            dispatch_status="assigned", lifecycle_stage="in_progress",
        )
        db.add(j)
        db.commit()
        _seed_assignment(db, job_id=j.id)
        body = client.get("/api/mobile/today").json()
        assert body["area_count"] == 1

    def test_other_techs_undated_jobs_excluded(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        db.add(Job(
            id=uuid4(), company_id=TENANT, customer_id=c.id,
            title="Someone else's", description="d",
            scheduled_at=None, assigned_to="tech-other",
            dispatch_status="assigned", lifecycle_stage="scheduled",
        ))
        db.commit()
        body = client.get("/api/mobile/today").json()
        assert body["area_count"] == 0

    def test_empty_response_keeps_area_shape(self, app_and_db):
        client, _ = app_and_db  # no technician record at all
        body = client.get("/api/mobile/today").json()
        assert body["area_jobs"] == []
        assert body["area_count"] == 0


class TestLocalTimezone:
    def test_evening_job_stays_on_local_day(self, app_and_db):
        """A 9pm America/Chicago job is 02:00 UTC the NEXT day. With the
        device tz it must appear on the local date; the UTC fallback used
        to push it to tomorrow (the 2026-07-10 'invisible evening job')."""
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        base = _now()  # 12:00 UTC today
        local_date = base.date()
        # 21:00 America/Chicago on local_date == 02:00 UTC on local_date+1.
        evening_utc = datetime(
            local_date.year, local_date.month, local_date.day, 21, 0,
            tzinfo=ZoneInfo("America/Chicago"),
        ).astimezone(UTC)
        j = _seed_job(db, customer_id=c.id, scheduled_at=evening_utc)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=evening_utc)

        with_tz = client.get(
            f"/api/mobile/today?date={local_date.isoformat()}&tz=America/Chicago"
        ).json()
        assert with_tz["count"] == 1

        utc_view = client.get(
            f"/api/mobile/today?date={local_date.isoformat()}"
        ).json()
        assert utc_view["count"] == 0

    def test_invalid_tz_falls_back_to_utc(self, app_and_db):
        client, db = app_and_db
        _seed_tech(db)
        c = _seed_customer(db)
        j = _seed_job(db, customer_id=c.id)
        _seed_appointment(db, job_id=j.id, customer_id=c.id, start=_now())
        body = client.get(
            f"/api/mobile/today?date={_now().date().isoformat()}&tz=Not/AZone"
        ).json()
        assert body["count"] == 1
