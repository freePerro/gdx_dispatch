"""GDXA-1545 — a soft-deleted job_assignments row is a removed tech.

Removing a tech from a crew soft-deletes their ``job_assignments`` row and
moves ``jobs.assigned_to`` off them, so that row is the only thing left
linking them to the job. Three mobile routers carried their own raw-SQL
fallback on ``job_assignments`` with no ``deleted_at`` filter, and the
removed row alone still granted access: chat read and post
(``mobile_chat._job_visible_to_user``), quoting
(``mobile_quoting._job_belongs_to_tech``) and the day summary's job lists.

Each test seeds a job assigned to another tech, so the assignment row is the
only path in. It proves the live row grants access (the control), then
soft-deletes it and expects the refusal. Schema from the ORM; the job id goes
over the wire dashed, as the SPA sends it and as the ORM writes
``job_assignments.job_id``.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    Job,
    JobAssignment,
    JobChatMessage,
    Technician,
)
from gdx_dispatch.routers import mobile_chat, mobile_day_summary, mobile_quoting
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"
OTHER_TECH = "tech-2"


def _noon_today() -> datetime:
    d = datetime.now(UTC).date()
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)


def _pg_engine(pg_test_db):
    """The ORM's schema in a schema of its own, as test_job_number_counter_behind
    does: ``pg_test_db``'s structure.sql trails the ORM (no jobs.job_number),
    so an ORM-built Job cannot be inserted into it."""
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.tenant_settings import Tenant, TenantSettings
    from gdx_dispatch.models.tenant_models import Base as TenantModelsBase

    schema = "removed_assignment_1545"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    TenantModelsBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Tenant.__table__.create(engine, checkfirst=True)
    TenantSettings.__table__.create(engine, checkfirst=True)
    return engine


@pytest.fixture(params=["sqlite", "pg"])
def db(request, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = _pg_engine(request.getfixturevalue("pg_test_db")) if request.param == "pg" else make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def pg_db(monkeypatch, pg_test_db):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = _pg_engine(pg_test_db)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _client(db, router) -> TestClient:
    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(router)
    user = {"user_id": USER, "tenant_id": TENANT, "role": "technician"}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = user
        return await call_next(request)

    return TestClient(app)


def _seed(db, *, scheduled_at: datetime | None = None) -> tuple[Job, JobAssignment]:
    now = datetime.now(UTC)
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True, created_at=now))
    db.add(Technician(id=OTHER_TECH, company_id=TENANT, user_id="user-2", active=True, created_at=now))
    db.commit()
    job = Job(
        id=uuid4(), company_id=TENANT, title="Spring",
        description="d", scheduled_at=scheduled_at or _noon_today(),
        assigned_to=OTHER_TECH, dispatch_status="assigned", lifecycle_stage="scheduled",
    )
    db.add(job)
    db.commit()
    ja = JobAssignment(id=str(uuid4()), job_id=str(job.id), tech_id=TECH, is_lead=False)
    db.add(ja)
    db.commit()
    return job, ja


def _remove(db, ja: JobAssignment) -> None:
    ja.deleted_at = datetime.now(UTC)
    db.commit()


def test_removed_tech_cannot_read_or_post_job_chat(db):
    job, ja = _seed(db)
    client = _client(db, mobile_chat.router)
    url = f"/api/mobile/jobs/{job.id}/chat"

    # Control: the live assignment is the only path in, and it works.
    assert client.get(url).status_code == 200
    assert client.post(url, json={"kind": "text", "body": "on site"}).status_code == 201

    _remove(db, ja)
    r = client.get(url)
    assert r.status_code == 404, r.text
    r = client.post(url, json={"kind": "text", "body": "still here?"})
    assert r.status_code == 404, r.text
    db.expire_all()
    assert db.query(JobChatMessage).count() == 1


def test_removed_tech_cannot_list_job_quotes(db):
    job, ja = _seed(db)
    client = _client(db, mobile_quoting.router)
    url = f"/api/mobile/jobs/{job.id}/quote"

    assert client.get(url).status_code == 200

    _remove(db, ja)
    r = client.get(url)
    assert r.status_code == 404, r.text


def test_removed_tech_job_is_not_tomorrows_first_stop(pg_db):
    # Postgres only: the day summary joins CAST(ja.job_id AS TEXT) =
    # CAST(j.id AS TEXT), and SQLite stores jobs.id as dashless hex while the
    # ORM writes job_assignments.job_id dashed, so on SQLite the live
    # assignment never matches and the control below cannot pass.
    db = pg_db
    _job, ja = _seed(db, scheduled_at=_noon_today() + timedelta(days=1))
    client = _client(db, mobile_day_summary.router)
    url = f"/api/mobile/day-summary?date={_noon_today().date().isoformat()}"

    assert client.get(url).json()["next_first_stop"] is not None

    _remove(db, ja)
    assert client.get(url).json()["next_first_stop"] is None


def test_removed_tech_day_summary_drops_the_job_and_its_invoice(pg_db):
    # Postgres for the same CAST join as above.
    db = pg_db
    job, ja = _seed(db)
    c = Customer(id=uuid4(), name="Acme", phone="555-1111", company_id=TENANT)
    db.add(c)
    job.customer_id = c.id
    job.lifecycle_stage = "completed"
    job.completed_at = _noon_today()
    db.add(Invoice(
        id=uuid4(), customer_id=c.id, job_id=job.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}", status="sent",
        subtotal=Decimal("100.00"), tax_amount=Decimal("0.00"), total=Decimal("100.00"),
        balance_due=Decimal("100.00"), public_token=secrets.token_urlsafe(48)[:64],
        company_id=TENANT, created_at=_noon_today(),
    ))
    db.commit()
    client = _client(db, mobile_day_summary.router)
    url = f"/api/mobile/day-summary?date={_noon_today().date().isoformat()}"

    body = client.get(url).json()
    assert body["jobs_completed_count"] == 1
    assert body["invoices_count"] == 1

    _remove(db, ja)
    body = client.get(url).json()
    assert body["jobs_completed_count"] == 0
    assert body["invoices_count"] == 0
