"""Diagnosis, hazard and receipt writes on a job leave an audit row (GDXA-1591).

Found by the sibling sweep of the vehicle-inspection fix: the same Sprint 5
handlers created, edited and soft-deleted rows with no audit call, so who
removed a hazard warning or a receipt could not be reconstructed (invariant
#1). Each audit row is flushed before ``db.commit()``, in the same transaction.

Through the real routers; each assertion reads ``audit_logs`` back.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Job, JobReceipt
from gdx_dispatch.routers import job_diagnosis as diagnosis_router
from gdx_dispatch.routers import job_hazards_receipts as hazards_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "dispatcher-1"


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    caller = {"user_id": USER, "sub": USER, "tenant_id": TENANT, "role": "admin"}

    app = FastAPI()
    app.include_router(diagnosis_router.router)
    app.include_router(hazards_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: caller

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
              description="", scheduled_at=datetime.now(UTC))
    db.add(job)
    db.commit()

    yield TestClient(app), db, str(job.id)
    db.close()
    engine.dispose()


def _audit(db, action: str) -> AuditLog:
    db.expire_all()
    (row,) = db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
    assert row.user_id == USER
    assert row.tenant_id == TENANT
    return row


def test_diagnosis_create_update_delete_each_audit(ctx):
    client, db, job_id = ctx
    r = client.post(f"/api/jobs/{job_id}/diagnosis",
                    json={"service_type": "broken_spring", "data": {"wire_gauge": "0.250"}})
    assert r.status_code == 201, r.text
    diag_id = r.json()["id"]
    row = _audit(db, "create_job_diagnosis")
    assert (row.entity_type, row.entity_id) == ("job_diagnosis", diag_id)
    assert row.details == {"job_id": job_id, "service_type": "broken_spring"}

    r = client.patch(f"/api/diagnosis/{diag_id}",
                     json={"service_type": "off_track", "data": {"wire_gauge": "0.250"},
                           "notes": "cable"})
    assert r.status_code == 200, r.text
    row = _audit(db, "update_job_diagnosis")
    assert row.entity_id == diag_id
    assert row.details == {
        "job_id": job_id,
        "changed": ["service_type", "notes"],
        "service_type": {"old": "broken_spring", "new": "off_track"},
    }

    r = client.delete(f"/api/diagnosis/{diag_id}")
    assert r.status_code == 200, r.text
    row = _audit(db, "delete_job_diagnosis")
    assert row.entity_id == diag_id
    assert row.details == {"job_id": job_id, "service_type": "off_track"}


def test_hazard_create_and_delete_each_audit(ctx):
    client, db, job_id = ctx
    r = client.post(f"/api/jobs/{job_id}/hazards",
                    json={"description": "Dog in yard", "severity": "high",
                          "applies_to_customer": True})
    assert r.status_code == 201, r.text
    haz_id = r.json()["id"]
    row = _audit(db, "create_job_hazard")
    assert (row.entity_type, row.entity_id) == ("job_hazard", haz_id)
    assert row.details == {"job_id": job_id, "severity": "high", "applies_to_customer": True}

    r = client.delete(f"/api/hazards/{haz_id}")
    assert r.status_code == 200, r.text
    row = _audit(db, "delete_job_hazard")
    assert row.entity_id == haz_id
    assert row.details == {"job_id": job_id, "severity": "high"}


def test_receipt_create_and_delete_each_audit(ctx):
    client, db, job_id = ctx
    r = client.post(f"/api/jobs/{job_id}/receipts",
                    json={"vendor": "Supply House", "amount": "41.17"})
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]
    row = _audit(db, "create_job_receipt")
    assert (row.entity_type, row.entity_id) == ("job_receipt", rec_id)
    assert row.details == {"job_id": job_id, "vendor": "Supply House", "amount": "41.17"}

    r = client.delete(f"/api/receipts/{rec_id}")
    assert r.status_code == 200, r.text
    row = _audit(db, "delete_job_receipt")
    assert row.entity_id == rec_id
    assert row.details["job_id"] == job_id


def test_failed_audit_takes_the_receipt_down_with_it(ctx, monkeypatch):
    client, db, job_id = ctx

    def _boom(*_a, **_k):
        raise SQLAlchemyError("audit insert refused")

    monkeypatch.setattr(hazards_router, "log_audit_event_sync", _boom)
    r = client.post(f"/api/jobs/{job_id}/receipts", json={"vendor": "X", "amount": "1.00"})
    assert r.status_code == 500
    db.expire_all()
    assert db.execute(select(JobReceipt)).scalars().all() == []
