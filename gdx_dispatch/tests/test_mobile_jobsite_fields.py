"""Mobile endpoints surface the effective JOBSITE, not the customer HQ.

PR 1 of docs/design/jobsite-address-visibility-plan.md. Every mobile
serializer was customer.address-only; a job bound to a customer_locations
row navigated the tech to the billing address. These tests pin the new
additive fields (site_label / site_address / site_address_missing), the
navigation_link source, the drive-time input, and the appointment-pin
provenance guard — endpoint-level, same harness as test_mobile_today.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    CustomerLocation,
    Job,
    Technician,
)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _now() -> datetime:
    today = datetime.now(UTC).date()
    return datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

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


def _seed(db, *, customer_address="100 Billing Rd"):
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    c = Customer(
        id=uuid4(), name="Acme", phone="555-1111", address=customer_address,
        company_id=TENANT,
    )
    db.add(c)
    db.commit()
    return c


def _job(db, customer_id: UUID, *, location_id=None, scheduled_at=None) -> Job:
    j = Job(
        id=uuid4(),
        company_id=TENANT,
        customer_id=customer_id,
        title="Install",
        description="desc",
        scheduled_at=scheduled_at or _now(),
        assigned_to=TECH,
        dispatch_status="assigned",
        location_id=location_id,
    )
    db.add(j)
    db.commit()
    return j


def _location(db, customer, *, label="Warehouse 3", address="9 Dock St",
              access_notes=None, lat=None, lng=None):
    loc = CustomerLocation(
        id=str(uuid4()), customer_id=str(customer.id), label=label,
        address=address, access_notes=access_notes, company_id=TENANT,
        lat=lat, lng=lng,
    )
    db.add(loc)
    db.commit()
    return loc


def _appt(db, job, customer, *, lat=None, lng=None, address=None) -> Appointment:
    a = Appointment(
        id=uuid4(), company_id=TENANT, job_id=job.id, customer_id=customer.id,
        tech_id=TECH, title="Today", start_at=_now(),
        end_at=_now() + timedelta(hours=1), lat=lat, lng=lng, address=address,
    )
    db.add(a)
    db.commit()
    return a


# ── /api/mobile/today ─────────────────────────────────────────────────


class TestTodayCards:
    def test_bound_location_wins_card_and_navigation(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St")
        _job(db, c.id, location_id=loc.id)
        r = client.get("/api/mobile/today").json()
        card = r["jobs"][0]
        assert card["site_address"] == "9 Dock St"
        assert card["site_label"] == "Warehouse 3"
        assert "9+Dock+St" in card["navigation_link"]
        assert "Billing" not in card["navigation_link"]
        # customer.address stays intact for anything still reading it.
        assert card["customer"]["address"] == "100 Billing Rd"

    def test_unbound_job_falls_back_to_customer(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        _job(db, c.id)
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["site_address"] == "100 Billing Rd"
        assert card["site_address_missing"] is False
        assert "100+Billing+Rd" in card["navigation_link"]

    def test_bound_location_without_address_is_missing_not_hq(self, app_and_db):
        """D2: never navigate the tech to the HQ off a label-only site."""
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address=None)
        _job(db, c.id, location_id=loc.id)
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["site_address"] is None
        assert card["site_address_missing"] is True
        assert card["navigation_link"] is None

    def test_area_jobs_get_site_fields_too(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St")
        j = _job(db, c.id, location_id=loc.id)
        j.scheduled_at = None
        j.lifecycle_stage = "scheduled"
        db.commit()
        r = client.get("/api/mobile/today").json()
        assert r["area_count"] == 1
        assert r["area_jobs"][0]["site_address"] == "9 Dock St"

    def test_pin_suppressed_when_provenance_unknown(self, app_and_db):
        """Bound site, appointment pin geocoded from an UNKNOWN address
        (appointment.address empty) — a wrong pin is worse than no pin."""
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St")
        j = _job(db, c.id, location_id=loc.id)
        _appt(db, j, c, lat=45.0, lng=-93.0)
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] is None

    def test_pin_repinned_from_bound_location_coords(self, app_and_db):
        """The location row's own lat/lng is ground truth — it beats the
        appointment's geocode (post-code audit §1/§2)."""
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St", lat=44.95, lng=-93.09)
        j = _job(db, c.id, location_id=loc.id)
        _appt(db, j, c, lat=45.0, lng=-93.0)
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] == {"lat": 44.95, "lng": -93.09}

    def test_pin_kept_when_appointment_geocoded_from_the_site(self, app_and_db):
        """Dispatch typed the site address on the appointment — its pin is
        trustworthy even without location coords (normalized compare)."""
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St")
        j = _job(db, c.id, location_id=loc.id)
        _appt(db, j, c, lat=45.0, lng=-93.0, address="9  dock st.")
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] == {"lat": 45.0, "lng": -93.0}

    def test_pin_suppressed_when_both_addresses_blank(self, app_and_db):
        """Blank==blank must not vacuously pass the provenance check — an
        address-less bound site with an address-less appointment pin would
        re-leak D2 through the map channel (verify-pass audit concern 1)."""
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address=None)
        j = _job(db, c.id, location_id=loc.id)
        _appt(db, j, c, lat=45.0, lng=-93.0)
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] is None

    def test_pin_kept_when_unbound(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        j = _job(db, c.id)
        _appt(db, j, c, lat=45.0, lng=-93.0)
        card = client.get("/api/mobile/today").json()["jobs"][0]
        assert card["location"] == {"lat": 45.0, "lng": -93.0}


# ── /api/mobile/job/{id} detail ───────────────────────────────────────


class TestJobDetail:
    def test_detail_site_fields_and_navigation(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St", access_notes="gate 4411")
        j = _job(db, c.id, location_id=loc.id)
        r = client.get(f"/api/mobile/job/{j.id.hex}")
        assert r.status_code == 200
        body = r.json()["job"]
        assert body["site_address"] == "9 Dock St"
        assert body["site_label"] == "Warehouse 3"
        assert body["site_access_notes"] == "gate 4411"
        assert body["site_source"] == "location"
        assert "9+Dock+St" in body["navigation_link"]
        assert body["customer"]["address"] == "100 Billing Rd"

    def test_detail_missing_address_never_links_to_hq(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address=None)
        j = _job(db, c.id, location_id=loc.id)
        body = client.get(f"/api/mobile/job/{j.id.hex}").json()["job"]
        assert body["site_address"] is None
        assert body["site_address_missing"] is True
        assert body["navigation_link"] is None


# ── /api/mobile/jobs list ─────────────────────────────────────────────


class TestJobsList:
    def test_list_emits_site_address(self, app_and_db):
        client, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St")
        _job(db, c.id, location_id=loc.id)
        r = client.get("/api/mobile/jobs").json()
        job = r["jobs"][0]
        assert job["site_address"] == "9 Dock St"
        assert job["site_label"] == "Warehouse 3"
        # customer_address unchanged — additive contract.
        assert job["customer_address"] == "100 Billing Rd"


# ── /api/mobile/day-summary next_first_stop ───────────────────────


class TestDaySummaryNextStop:
    """Tomorrow's first stop is navigation — it must obey D2 like every
    other surface (post-code audit 2026-08-18 §5: this screen reproduced
    the Jobs-tab HQ-fallback bug)."""

    def _client(self, db):
        from gdx_dispatch.core.modules import require_module
        from gdx_dispatch.routers import mobile_day_summary as ds
        app = FastAPI()
        app.include_router(ds.router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": USER, "tenant_id": TENANT, "role": "technician",
        }
        app.dependency_overrides[require_module("mobile")] = lambda: True

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.tenant = {"id": TENANT, "slug": "test"}
            request.state.user = {"user_id": USER, "tenant_id": TENANT}
            return await call_next(request)

        return TestClient(app)

    def _tomorrow_job(self, db, c, **kw):
        j = _job(db, c.id, **kw)
        j.scheduled_at = _now() + timedelta(days=1)
        j.assigned_to = USER  # day-summary matches assigned_to against user id
        db.commit()
        return j

    def test_next_stop_shows_bound_site(self, app_and_db):
        _, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address="9 Dock St")
        self._tomorrow_job(db, c, location_id=loc.id)
        r = self._client(db).get("/api/mobile/day-summary").json()
        stop = r["next_first_stop"]
        assert stop["site_address"] == "9 Dock St"
        assert stop["site_address_missing"] is False

    def test_next_stop_bound_missing_never_falls_to_hq(self, app_and_db):
        _, db = app_and_db
        c = _seed(db)
        loc = _location(db, c, address=None)
        self._tomorrow_job(db, c, location_id=loc.id)
        r = self._client(db).get("/api/mobile/day-summary").json()
        stop = r["next_first_stop"]
        assert stop["site_address"] is None
        assert stop["site_address_missing"] is True
        # customer_address still present for the recap — the CLIENT gates on
        # the missing flag (MobileSummaryView renders ask-dispatch).
        assert stop["customer_address"] == "100 Billing Rd"
