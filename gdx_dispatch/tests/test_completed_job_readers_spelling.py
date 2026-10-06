"""GDXA-227: completed jobs are lifecycle_stage='completed', whatever the
status string says, and an undated completed job is surfaced, not dropped.

Prod 2026-10-05, lifecycle_stage='completed': 254 rows (172 status NULL,
39 'Complete', 43 'Completed'), of which 240 are not soft-deleted (162 /
35 / 43); 47 carry completed_at. Readers
keyed on the status spelling, or on completed_at alone, silently dropped the
first two groups. Each test below seeds the historical shapes and was red
before the fix (status-keyed filter / bare completed_at check).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router
from gdx_dispatch.routers.service_calls import router as service_calls_router

TENANT_ID = "00000000-0000-4000-8000-0000000000aa"


@pytest.fixture
def harness():
    engine = make_fresh_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-{TENANT_ID}", "tid": TENANT_ID},
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
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT_ID}
        request.state.request_id = "gdxa-227"
        return await call_next(request)

    for r in (jobs_router, service_calls_router):
        app.include_router(r)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": TENANT_ID,
    }
    yield TestClient(app, raise_server_exceptions=True), Session
    engine.dispose()


def _job(Session, **kw) -> str:
    db = Session()
    try:
        job = Job(id=uuid4(), company_id=TENANT_ID, title=kw.pop("title", "job"), **kw)
        db.add(job)
        db.commit()
        return str(job.id)
    finally:
        db.close()


def test_callback_with_undated_completed_parent_is_undetermined(harness):
    tc, Session = harness
    parent = _job(Session, title="parent", lifecycle_stage="completed", status=None)
    child = _job(Session, title="child", lifecycle_stage="scheduled", parent_job_id=UUID(parent))
    body = tc.get(f"/api/jobs/{child}").json()
    assert body["is_callback"] is False
    assert body["callback_undetermined"] is True


def test_callback_with_dated_parent_is_detected(harness):
    """The ORM read also fixes the raw `id = :pid` bind that never matched
    SQLite's dashless Uuid storage, so this path now runs in the suite."""
    tc, Session = harness
    done = datetime.now(UTC) - timedelta(days=10)
    parent = _job(Session, title="parent", lifecycle_stage="completed",
                  status="Completed", completed_at=done)
    child = _job(Session, title="child", lifecycle_stage="scheduled",
                 parent_job_id=UUID(parent), scheduled_at=datetime.now(UTC))
    body = tc.get(f"/api/jobs/{child}").json()
    assert body["is_callback"] is True
    assert body["callback_undetermined"] is False


def test_service_call_queue_keeps_null_status_open_and_drops_completed(harness):
    tc, Session = harness
    open_null = _job(Session, title="open", job_type="Repair",
                     lifecycle_stage="service_call", status=None)
    done_null = _job(Session, title="done", job_type="Repair",
                     lifecycle_stage="completed", status=None)
    done_spelled = _job(Session, title="legacy", job_type="Repair",
                        lifecycle_stage="scheduled", status="Complete")
    r = tc.get("/api/service-calls/active")
    assert r.status_code == 200, r.text
    open_spelled = _job(Session, title="open2", job_type="Repair",
                        lifecycle_stage="scheduled", status="Scheduled")
    r = tc.get("/api/service-calls/active")
    # Counted, not a set: the endpoint once emitted every row once per row.
    ids = [row["id"] for row in r.json()]
    assert sorted(ids) == sorted([open_null, open_spelled])
    assert done_null and done_spelled  # seeded, and excluded above


def test_duration_estimate_samples_null_status_completed_jobs():
    """The job-type average sampled only status 'Completed'/'completed', so a
    type whose completed history is all NULL-status had no estimate at all."""
    from test_time_entry_soft_delete_readers import (
        TENANT,
        _app,
        _engine_and_sessions,
        _insert_time_entry,
    )

    SessionLocal, engine = _engine_and_sessions()
    try:
        now = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
        with SessionLocal() as db:
            target = Job(id=uuid4(), company_id=TENANT, title="t",
                         status="Scheduled", job_type="repair")
            imported = Job(id=uuid4(), company_id=TENANT, title="i", status=None,
                           lifecycle_stage="completed", job_type="repair")
            db.add_all([target, imported])
            db.flush()
            _insert_time_entry(db, imported.id, duration_minutes=90, clock_in=now)
            db.commit()
            target_hex = target.id.hex
        r = _app(jobs_router, SessionLocal).get(f"/api/jobs/{target_hex}/duration")
        assert r.status_code == 200, r.text
        assert r.json()["estimated_hours"] == 1.5
        assert r.json()["sample_size"] == 1
    finally:
        engine.dispose()


def test_can_start_counts_null_status_completed_dependency_as_done(pg_test_session):
    """Postgres only: the gate's `CAST(... AS uuid)` join never matches on
    SQLite (NUMERIC affinity against dashless hex), so a SQLite run cannot
    tell done from missing. The PG arm skips with no reachable Postgres and
    fails under CI (#440) — read -rs."""
    from types import SimpleNamespace

    from gdx_dispatch.routers.jobs import can_start_job

    db = pg_test_session
    # Raw insert of the columns the gate reads: the PG fixture's
    # structure.sql trails the ORM (no jobs.job_number), so an ORM add fails.
    target, done_null, open_null = uuid4(), uuid4(), uuid4()
    for jid, stage in ((target, "scheduled"), (done_null, "completed"), (open_null, "scheduled")):
        db.execute(
            text(
                "INSERT INTO jobs (id, title, lifecycle_stage, dispatch_status, company_id, status) "
                "VALUES (:id, 't', CAST(:stage AS job_lifecycle_stage), 'unassigned', :tid, NULL)"
            ),
            {"id": jid, "stage": stage, "tid": TENANT_ID},
        )

    def _dep(on):
        db.execute(
            text(
                "INSERT INTO job_dependencies (id, tenant_id, job_id, depends_on_job_id, created_at) "
                "VALUES (:id, :tid, :jid, :on, 'now')"
            ),
            {"id": str(uuid4()), "tid": TENANT_ID, "jid": str(target), "on": str(on)},
        )

    req = SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT_ID}))

    def _gate():
        import json

        return json.loads(can_start_job(str(target), req, {"sub": "u"}, db).body)

    _dep(done_null)
    assert _gate() == {"can_start": True, "blocking_count": 0}

    _dep(open_null)
    assert _gate() == {"can_start": False, "blocking_count": 1}

    _dep(uuid4())  # a dependency on a job that does not exist still blocks
    assert _gate() == {"can_start": False, "blocking_count": 2}
