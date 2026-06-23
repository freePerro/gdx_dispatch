"""Wave G — call→job linkage + cold-leads endpoint tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.phone_com.models import PhoneComCall, PhoneComVoicemail
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.modules.phone_com.router import router as phone_com_router


@pytest.fixture
def db_session():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    return sm()


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.dependency_overrides[require_module("phone_com")] = lambda: True
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_tenant_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": str(uuid4()), "tenant_id": str(uuid4()), "role": "admin",
    }
    app.include_router(phone_com_router)
    return TestClient(app)


def _seed_call(db, **kw):
    call = PhoneComCall(
        phone_com_call_id=kw.get("phone_com_call_id", str(uuid4())),
        direction=kw.get("direction", "in"),
        from_number=kw.get("from_number", "+15551234567"),
        to_number=kw.get("to_number", "+18005550199"),
        started_at=kw.get("started_at", datetime.now(timezone.utc)),
        duration_s=kw.get("duration_s", 60),
        status=kw.get("status", "voicemail"),
        customer_id=kw.get("customer_id"),
        raw_payload=kw.get("raw_payload", {}),
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    return call


def test_link_job_sets_job_id(client, db_session):
    call = _seed_call(db_session)
    job = Job(
        id=uuid4(),
        title="Repair garage door",
        lifecycle_stage="scheduled",
        customer_id=None,
        company_id="test-company",
    )
    db_session.add(job)
    db_session.commit()

    r = client.patch(f"/api/phone-com/calls/{call.id}/job", json={"job_id": str(job.id)})
    assert r.status_code == 200
    assert r.json()["job_id"] == str(job.id)

    db_session.refresh(call)
    assert call.job_id == job.id


def test_link_job_unlink_with_null(client, db_session):
    job = Job(id=uuid4(), title="J", lifecycle_stage="scheduled", customer_id=None, company_id="test-company")
    db_session.add(job)
    db_session.commit()
    call = _seed_call(db_session, raw_payload={})
    call.job_id = job.id
    db_session.commit()

    r = client.patch(f"/api/phone-com/calls/{call.id}/job", json={"job_id": None})
    assert r.status_code == 200
    assert r.json()["job_id"] is None
    db_session.refresh(call)
    assert call.job_id is None


def test_link_job_404_on_unknown_job(client, db_session):
    call = _seed_call(db_session)
    r = client.patch(
        f"/api/phone-com/calls/{call.id}/job",
        json={"job_id": str(uuid4())},
    )
    assert r.status_code == 404


def test_link_job_404_on_unknown_call(client):
    r = client.patch(
        f"/api/phone-com/calls/{uuid4()}/job",
        json={"job_id": None},
    )
    assert r.status_code == 404


def test_cold_leads_groups_by_from_number(client, db_session):
    base = datetime.now(timezone.utc)
    # Two unmatched calls from same number
    _seed_call(db_session, from_number="+15551111111", duration_s=120, started_at=base)
    _seed_call(db_session, from_number="+15551111111", duration_s=80, started_at=base - timedelta(days=1))
    # One unmatched call from different number
    _seed_call(db_session, from_number="+15552222222", duration_s=30, started_at=base - timedelta(hours=2))
    # One matched call (should be excluded). Skip creating a real Customer
    # row — cold_leads filters on customer_id IS NULL, no JOIN needed, so a
    # synthetic UUID suffices and dodges the legacy customers.company_id
    # NOT NULL constraint.
    _seed_call(db_session, from_number="+15553333333", duration_s=60, customer_id=uuid4())
    # One short misdial (excluded by min_duration_s default=10)
    _seed_call(db_session, from_number="+15554444444", duration_s=2)

    r = client.get("/api/phone-com/cold-leads")
    assert r.status_code == 200
    body = r.json()
    nums = sorted(item["from_number"] for item in body["items"])
    assert nums == ["+15551111111", "+15552222222"]
    by_num = {item["from_number"]: item for item in body["items"]}
    assert by_num["+15551111111"]["call_count"] == 2
    assert by_num["+15552222222"]["call_count"] == 1


def test_cold_leads_min_duration_zero_includes_misdials(client, db_session):
    _seed_call(db_session, from_number="+15554444444", duration_s=2)
    r = client.get("/api/phone-com/cold-leads?min_duration_s=0")
    assert r.status_code == 200
    nums = [i["from_number"] for i in r.json()["items"]]
    assert "+15554444444" in nums


def test_cold_leads_voicemail_snippet_surfaces(client, db_session):
    base = datetime.now(timezone.utc)
    call = _seed_call(db_session, from_number="+15558887777", duration_s=60, started_at=base)
    db_session.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-1",
        call_id=call.id,
        transcript="please call me back about the broken spring",
        raw_payload={},
    ))
    db_session.commit()

    r = client.get("/api/phone-com/cold-leads")
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["from_number"] == "+15558887777")
    assert "broken spring" in item["voicemail_snippet"]
