"""A job-number counter behind the numbers already issued — GDXA-924.

The counter (``tenant_settings.job_number_next_seq``) can fall behind the
numbers on live jobs: rewound in Settings, or a restored database. The
allocator rendered the counter and handed it out unchecked, so the job INSERT
hit ``ux_jobs_job_number_live`` and POST /api/jobs answered 500
``create_job_failed``, burning a counter value per attempt.

These run on Postgres because ``next_job_number`` locks the settings row
``FOR UPDATE``, which SQLite cannot parse. The schema is built from the ORM
into a schema of its own (``pg_test_db``'s ``structure.sql`` is older than
``jobs.job_number``), and the partial unique index uses migration 048's exact
predicate, so a regression reproduces the real 500 rather than a silent
duplicate.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

YEAR = datetime.now(UTC).year


def _num(seq: int) -> str:
    return f"JOB-{YEAR}-{seq:03d}"


@pytest.fixture
def pg(pg_test_db):
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.tenant_settings import Tenant, TenantSettings
    from gdx_dispatch.models.tenant_models import Base as TenantModelsBase

    schema = "counter_924"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    TenantModelsBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Tenant.__table__.create(engine, checkfirst=True)
    TenantSettings.__table__.create(engine, checkfirst=True)
    with engine.begin() as c:
        c.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_job_number_live ON jobs (job_number) "
            "WHERE deleted_at IS NULL AND job_number IS NOT NULL"
        )
    tid = uuid.uuid4()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with Session() as s:
        s.add(Tenant(id=tid, slug="t924", name="t924"))
        s.flush()
        s.add(TenantSettings(tenant_id=tid, job_number_next_seq=2, job_number_year_seen=YEAR))
        s.commit()
    try:
        yield engine, Session, tid
    finally:
        engine.dispose()


def _add_job(engine, tid, number: str, *, deleted: bool = False) -> None:
    from gdx_dispatch.models.tenant_models import Job

    with sessionmaker(bind=engine)() as s:
        s.add(Job(id=uuid.uuid4(), title=f"existing {number}", job_number=number, company_id=str(tid),
                  deleted_at=datetime.now(UTC) if deleted else None))
        s.commit()


def _counter(engine) -> int:
    with engine.connect() as c:
        return c.execute(text("SELECT job_number_next_seq FROM tenant_settings")).scalar()


def test_next_job_number_skips_numbers_already_issued(pg):
    from gdx_dispatch.modules.numbering import next_job_number

    engine, Session, tid = pg
    for seq in (2, 3, 5):
        _add_job(engine, tid, _num(seq))
    # A soft-deleted job keeps its number: POs, emails and audit rows quote it.
    _add_job(engine, tid, _num(6), deleted=True)

    with Session() as cdb:
        first = next_job_number(cdb, tid)
        cdb.commit()
    assert first == _num(4)
    assert _counter(engine) == 5

    with Session() as cdb:
        second = next_job_number(cdb, str(tid))
        cdb.commit()
    assert second == _num(7)
    assert _counter(engine) == 8


def test_counter_behind_max_existing_lands_past_it(pg, monkeypatch):
    import gdx_dispatch.core.database as database
    from gdx_dispatch.modules.numbering import allocate_job_number

    engine, Session, tid = pg
    for seq in range(1, 10):
        _add_job(engine, tid, _num(seq))
    monkeypatch.setattr(database, "SessionLocal", Session)
    assert allocate_job_number(tid) == _num(10)
    assert _counter(engine) == 11


def test_exhausted_template_leaves_the_counter_alone(pg):
    """A template with no {seq} renders one number forever: refuse, don't spin or advance."""
    from gdx_dispatch.modules.numbering import next_job_number
    from gdx_dispatch.modules.numbering.service import NumberingExhausted

    engine, Session, tid = pg
    with engine.begin() as c:
        c.execute(text("UPDATE tenant_settings SET job_number_format = 'FIXED'"))
    _add_job(engine, tid, "FIXED")
    with Session() as cdb, pytest.raises(NumberingExhausted):
        next_job_number(cdb, tid)
    assert _counter(engine) == 2


def _client(monkeypatch, Session, tid, *, role: str, routers):
    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user

    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    tenant = str(tid)
    db = Session()
    db.execute(
        text(
            "INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', now(), now())"
        ),
        {"id": str(uuid.uuid4()), "tid": tenant},
    )
    db.commit()
    caller = {"user_id": str(uuid.uuid4()), "tenant_id": tenant, "role": role, "email": "a@x.com"}
    caller["sub"] = caller["user_id"]
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": tenant, "slug": "t924"}
        request.state.tenant_id = tenant
        request.state.user = caller
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), db


def test_post_job_with_counter_behind_gets_a_fresh_number(pg, monkeypatch):
    """The reported failure: 500 create_job_failed. Now 201 with the next free number."""
    from gdx_dispatch.routers.jobs import router as jobs_router

    engine, Session, tid = pg
    for seq in (2, 3):
        _add_job(engine, tid, _num(seq))
    monkeypatch.setattr("gdx_dispatch.routers.jobs.SessionLocal", Session)
    client, db = _client(monkeypatch, Session, tid, role="dispatcher", routers=[jobs_router])
    try:
        r = client.post("/api/jobs", json={"title": "Spring broken"})
        assert r.status_code == 201, r.text[:400]
        with engine.connect() as c:
            numbers = c.execute(
                text("SELECT job_number FROM jobs WHERE title = 'Spring broken'")
            ).scalars().all()
        assert numbers == [_num(4)]
        assert _counter(engine) == 5
    finally:
        db.close()


def test_patch_config_refuses_a_seq_already_issued(pg, monkeypatch):
    from gdx_dispatch.modules.numbering.router import router as numbering_router

    engine, Session, tid = pg
    for seq in (2, 3):
        _add_job(engine, tid, _num(seq))
    _add_job(engine, tid, _num(4), deleted=True)
    client, db = _client(monkeypatch, Session, tid, role="admin", routers=[numbering_router])
    try:
        r = client.patch("/api/numbering/config",
                         json={"job_number_format": "JOB-{year}-{seq:03d}", "job_number_next_seq": 2})
        assert r.status_code == 400, r.text[:400]
        detail = r.json()["detail"]
        assert _num(2) in detail and _num(5) in detail
        assert _counter(engine) == 2  # refused: nothing written

        on_deleted = client.patch("/api/numbering/config",
                                  json={"job_number_format": "JOB-{year}-{seq:03d}", "job_number_next_seq": 4})
        assert on_deleted.status_code == 400, on_deleted.text[:400]

        ok = client.patch("/api/numbering/config",
                          json={"job_number_format": "JOB-{year}-{seq:03d}", "job_number_next_seq": 5})
        assert ok.status_code == 200, ok.text[:400]
        assert ok.json()["job_number_next_seq"] == 5
        assert _counter(engine) == 5
        with engine.connect() as c:
            audited = c.execute(
                text("SELECT count(*) FROM audit_logs WHERE action = 'numbering_config_updated'")
            ).scalar()
        assert audited == 1
    finally:
        db.close()


def test_patch_config_refuses_a_format_without_seq(pg, monkeypatch):
    """A fixed format numbers one job; every later create would go unnumbered."""
    from gdx_dispatch.modules.numbering.router import router as numbering_router

    engine, Session, tid = pg
    client, db = _client(monkeypatch, Session, tid, role="admin", routers=[numbering_router])
    try:
        for fmt in ("GDX", "{customer_initials}-{year}"):
            r = client.patch("/api/numbering/config",
                             json={"job_number_format": fmt, "job_number_next_seq": 2})
            assert r.status_code == 400, (fmt, r.text[:400])
            assert "{seq}" in r.json()["detail"]
        with engine.connect() as c:
            assert c.execute(text("SELECT job_number_format FROM tenant_settings")).scalar() != "GDX"
    finally:
        db.close()
