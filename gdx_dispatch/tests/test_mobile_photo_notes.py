"""Sprint tech_mobile S1-B3 + S1-B4 — photo slot tagging + notes attribution.

S1-B3 — POST /api/mobile/jobs/{id}/photos accepts a ``kind`` form field
({before, during, after}); when tech_mobile.photo_slot_tagging is
"required" the field is mandatory. The uploader's user_id stamps
JobPhoto.uploaded_by; EXIF GPS + capture_time are extracted from the
image bytes and recorded on the audit row.

S1-B4 — POST /api/mobile/jobs/{id}/notes populates JobNote.author_name
from the calling user's display fields (name / full_name / email).
"""
from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    AppSettings,
    Customer,
    Job,
    JobNote,
    Technician,
)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db


@pytest.fixture(autouse=True)
def _photo_upload_dir(tmp_path, monkeypatch):
    """Point UPLOAD_DIR somewhere writable for every test in this module.

    The mobile photo route used to write to MOBILE_UPLOAD_DIR (default
    /tmp/gdx_mobile_uploads) and mint a url nothing served. It now stores bytes
    in the SAME flat document root the download route reads, whose default is
    /app/uploads — writable in the dev container, not on a CI runner, where
    these tests failed with PermissionError: '/app'. Tests that write files
    must say where; relying on a default that happens to be writable is how
    this went unnoticed.
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))


TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _now() -> datetime:
    return datetime.now(UTC)


def _png_bytes() -> bytes:
    """Smallest valid PNG (1x1 transparent pixel)."""
    return bytes.fromhex(
        "89504e470d0a1a0a"
        "0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000100"
        "0d0a2db40000000049454e44ae426082"
    )


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()

    from gdx_dispatch.core.modules import require_module

    user_dict = {
        "user_id": USER,
        "tenant_id": TENANT,
        "role": "technician",
        "name": "Diego Tech",
        "email": "diego@example.com",
    }

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = lambda: user_dict
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = user_dict
        return await call_next(request)

    client = TestClient(app)
    yield client, s, user_dict
    s.close()
    engine.dispose()


def _seed_job(db) -> Job:
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    cust = Customer(
        id=uuid4(),
        name="Acme",
        phone="555",
        email="a@x.com",
        address="11 Main",
        company_id=TENANT,
    )
    db.add(cust)
    job = Job(
        id=uuid4(),
        company_id=TENANT,
        customer_id=cust.id,
        title="Fix",
        description="",
        scheduled_at=_now(),
        assigned_to=TECH,
        dispatch_status="on_site",
    )
    db.add(job)
    db.commit()
    return job


def _set_setting(db, key: str, value):
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(tenant_mobile_settings={key: value})
        db.add(row)
    else:
        overrides = dict(row.tenant_mobile_settings or {})
        overrides[key] = value
        row.tenant_mobile_settings = overrides
    db.commit()


# ── B3 photos: the writer is gone ────────────────────────────────────


class TestMobilePhotoWritersAreRetired:
    """The two mobile photo-upload routes were deleted 2026-08-25.

    They had no caller: the mobile app uploads through ``POST /api/documents``
    (``usePhotoQueue``), and prod had produced zero rows through them. The one
    thing they did that nothing else does — pull EXIF GPS and capture time —
    is unreachable in practice: 0 of 183 stored prod images carried either,
    because iOS strips location from photo-library uploads by design (WebKit
    #207088; opt-in only in the iOS 17+ picker).

    This asserts their ABSENCE. A presence test would only prove someone typed
    the route name; absence is the property that can actually regress, and it
    does so the moment somebody "restores" the endpoint instead of pointing a
    caller at /api/documents.
    """

    def test_both_writer_routes_are_gone(self, app_and_db):
        """Refused, whatever shape the refusal takes.

        Do NOT tighten this back to ``== 404``. It read that way when written
        and passed here, but the deployed app answers **405** — walked on prod
        2026-08-26. The SPA catch-all is registered for GET on
        ``/{full_path:path}``, so ANY unmatched POST matches the path and fails
        on the method: a path that has never existed returns 405 too. The test
        app has no catch-all, so it returns 404 and the stricter assertion
        looked correct while encoding a status production never produces.

        What actually matters is that the route does not do the work — so
        assert "not success", which is true in both environments.
        """
        client, db, _ = app_and_db
        job = _seed_job(db)
        for path in (
            f"/api/mobile/jobs/{job.id.hex}/photos",
            f"/api/mobile/job/{job.id.hex}/photo",
        ):
            r = client.post(path, files={"file": ("p.png", _png_bytes(), "image/png")})
            assert r.status_code in (404, 405), f"{path} answers {r.status_code}"
            assert not r.is_success, f"{path} still accepted an upload"

    def test_the_handler_and_its_exif_helper_are_gone(self):
        """Deleting the routes but leaving the code is half a deletion."""
        from gdx_dispatch.routers import mobile as mobile_router

        for symbol in (
            "upload_mobile_job_photo",
            "_photo_exif_metadata",
            "_VALID_PHOTO_KINDS",
        ):
            assert not hasattr(mobile_router, symbol), f"{symbol} survived"

    def test_no_mobile_photo_route_is_registered(self):
        """The status-code check above is environment-sensitive; this is not.

        Walks the real route table the way tests/conftest.py::iter_app_routes
        does — a flat ``app.routes`` scan cannot see routes behind the lazily
        included routers, and would pass vacuously.
        """
        from gdx_dispatch.app import app
        from gdx_dispatch.tests.conftest import iter_app_routes

        offenders = [
            f"{sorted(r.methods or [])} {path}"
            for path, r in iter_app_routes(app)
            if "mobile" in path and "photo" in path
        ]
        assert offenders == [], f"mobile photo routes still registered: {offenders}"


# ── B4 notes ──────────────────────────────────────────────────────────


class TestNotesAttribution:
    def test_author_id_and_name_populated(self, app_and_db):
        client, db, _ = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/notes",
            json={"note": "Customer wants to reschedule"},
        )
        assert r.status_code == 201, r.text
        row = db.query(JobNote).filter(JobNote.job_id == str(job.id)).one()
        assert row.author_id == USER
        # User dict carries name="Diego Tech"; helper picks `name` first.
        assert row.author_name == "Diego Tech"

    def test_falls_back_to_email_when_no_name(self, app_and_db, monkeypatch):
        client, db, _ = app_and_db
        # Override the dep to drop the name field so the helper falls
        # back to email.
        client.app.dependency_overrides[get_current_user] = lambda: {
            "user_id": USER,
            "tenant_id": TENANT,
            "role": "technician",
            "email": "noname@example.com",
        }
        job = _seed_job(db)
        client.post(
            f"/api/mobile/jobs/{job.id.hex}/notes",
            json={"note": "n"},
        )
        row = db.query(JobNote).filter(JobNote.job_id == str(job.id)).one()
        assert row.author_name == "noname@example.com"
