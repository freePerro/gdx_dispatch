"""Sprint tech_mobile S1-B5 — signature surface + required setting on /complete.

Two settings gate the completion flow:
- ``tech_mobile.signature_required_completion`` (required / optional / off)
- ``tech_mobile.signature_surface`` (phone_handoff / customer_link)

phone_handoff (default) — customer signs on the tech's device; signature_data
must accompany the /complete request when required=required.

customer_link — out of scope for this sprint slice; /complete returns 501
with a clear next-step.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Appointment, AppSettings, Customer, Job, Technician
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db


TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"
SIG_DATA = "data:image/png;base64," + base64.b64encode(b"signed").decode()


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()

    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: s
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
    yield client, s
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


# ── Signature required setting ────────────────────────────────────────


class TestSignatureRequired:
    def test_default_required_blocks_unsigned_complete(self, app_and_db):
        # Catalog default for signature_required_completion = "required";
        # a /complete with no signature_data + no existing signature must
        # 400.
        client, db = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"completion_notes": "done"},
        )
        assert r.status_code == 400
        assert "ignature" in r.json()["detail"]

    def test_required_accepts_signed_complete(self, app_and_db):
        client, db = app_and_db
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"signature_data": SIG_DATA, "signed_by": "Cust"},
        )
        assert r.status_code == 200
        assert r.json()["dispatch_status"] == "done"

    def test_optional_accepts_unsigned_complete(self, app_and_db):
        client, db = app_and_db
        _set_setting(db, "tech_mobile.signature_required_completion", "optional")
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"completion_notes": "no sig today"},
        )
        assert r.status_code == 200, r.text

    def test_off_discards_sent_signature(self, app_and_db):
        # When the setting is "off", any signature in the request body
        # must NOT land on the Job — the tenant has explicitly opted
        # out of capturing them.
        client, db = app_and_db
        _set_setting(db, "tech_mobile.signature_required_completion", "off")
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"signature_data": SIG_DATA, "signed_by": "Cust"},
        )
        assert r.status_code == 200
        db.expire_all()
        # signature_data column on Job is read via raw SQL elsewhere; confirm
        # nothing landed by re-querying.
        sig_col = db.execute(
            mobile_router._text(
                "SELECT signature_data FROM jobs WHERE id = :id"
            ),
            {"id": str(job.id)},
        ).scalar()
        assert not sig_col


# ── Signature surface setting ────────────────────────────────────────


class TestSignatureSurface:
    def test_customer_link_returns_501(self, app_and_db):
        client, db = app_and_db
        _set_setting(db, "tech_mobile.signature_surface", "customer_link")
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"signature_data": SIG_DATA, "signed_by": "Cust"},
        )
        assert r.status_code == 501
        assert "not yet implemented" in r.json()["detail"]

    def test_phone_handoff_works(self, app_and_db):
        client, db = app_and_db
        _set_setting(db, "tech_mobile.signature_surface", "phone_handoff")
        job = _seed_job(db)
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"signature_data": SIG_DATA, "signed_by": "Cust"},
        )
        assert r.status_code == 200
