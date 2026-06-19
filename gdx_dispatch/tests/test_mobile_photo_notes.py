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

from gdx_dispatch.core.audit import AuditLog
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


# ── B3 photos ─────────────────────────────────────────────────────────


class TestPhotoSlotTagging:
    def test_default_kind_during_when_setting_optional(self, app_and_db):
        client, db, _ = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/photos",
            files={"file": ("p.png", _png_bytes(), "image/png")},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "during"

    def test_explicit_kind_before(self, app_and_db):
        client, db, _ = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/photos",
            files={"file": ("p.png", _png_bytes(), "image/png")},
            data={"kind": "before"},
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "before"

    def test_invalid_kind_400(self, app_and_db):
        client, db, _ = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/photos",
            files={"file": ("p.png", _png_bytes(), "image/png")},
            data={"kind": "sideways"},
        )
        assert r.status_code == 400
        assert "must be one of" in r.json()["detail"]

    def test_kind_required_setting_rejects_missing(self, app_and_db):
        client, db, _ = app_and_db
        _set_setting(db, "tech_mobile.photo_slot_tagging", "required")
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/photos",
            files={"file": ("p.png", _png_bytes(), "image/png")},
        )
        assert r.status_code == 400
        assert "kind is required" in r.json()["detail"]

    def test_uploaded_by_stamped_on_row(self, app_and_db):
        client, db, _ = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/photos",
            files={"file": ("p.png", _png_bytes(), "image/png")},
            data={"kind": "after"},
        )
        body = r.json()
        assert body["uploaded_by"] == USER

    def test_audit_row_carries_kind_and_uploader(self, app_and_db):
        client, db, _ = app_and_db
        job = _seed_job(db)
        client.post(
            f"/api/mobile/jobs/{job.id.hex}/photos",
            files={"file": ("p.png", _png_bytes(), "image/png")},
            data={"kind": "after"},
        )
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "upload_mobile_job_photo")
            .all()
        )
        assert len(rows) >= 1
        details = rows[-1].details
        assert details["kind"] == "after"
        assert details["uploaded_by"] == USER


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
