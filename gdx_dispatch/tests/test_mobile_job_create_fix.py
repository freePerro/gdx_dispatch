"""Mobile job-create fix (2026-07-22) — regression suite.

Doug's report: "creating a new job [from mobile] does not work correctly
or save." Three real defects, each pinned here:

1. ``description`` was silently dropped — ``JobCreate`` never declared the
   field, pydantic discarded it, the Job row never got it.
2. ``JobCreate.status`` defaulted to "Scheduled", so the handler's
   ``derived_status`` ("Service Call" for date-less jobs) was dead code
   and unscheduled service calls were stamped status="Scheduled" while
   lifecycle_stage said service_call.
3. The created job was invisible to its creator: ``/api/mobile/jobs``
   matched only assignment (and early-returned empty for callers with no
   technician row), while dialog-created jobs have no tech until dispatch
   assigns one. Fix: ``jobs.created_by`` (migration 035) + a
   creator-while-unassigned visibility clause.

The /audit of the fix plan demanded the read/write split be pinned too:
creator visibility is READ-ONLY and expires on assignment —
``job_belongs_to_user`` (the write gate for start/complete/clock/status)
must NOT match on created_by. Those assertions live here as well.

Harnesses mirror the established patterns: ``test_jobs_endpoints_smoke``
(TestClient + in-memory SQLite for create_job) and ``test_mobile_api``
(direct handler invocation for the mobile router).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from conftest import make_fresh_db
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.job_access import job_belongs_to_user
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router

TENANT_ID = "00000000-0000-4000-8000-0000000000ab"


# ─── Part A: create_job contract (description / status / created_by) ───────


@pytest.fixture
def client():
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
        request.state.request_id = "mobile-fix"
        return await call_next(request)

    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-creator",
        "sub": "user-creator",
        "role": "technician",
        "tenant_id": TENANT_ID,
    }
    tc = TestClient(app, raise_server_exceptions=True)
    tc._session_factory = Session  # let tests inspect the row that was written
    return tc


def _job_row(client: TestClient, job_id: str) -> dict:
    db = client._session_factory()
    try:
        row = db.execute(
            text(
                "SELECT description, status, lifecycle_stage, created_by "
                "FROM jobs WHERE CAST(id AS TEXT) = :id OR id = :id"
            ),
            {"id": job_id},
        ).mappings().first()
        assert row is not None, f"job {job_id} not found in DB"
        return dict(row)
    finally:
        db.close()


def test_create_job_persists_description(client: TestClient) -> None:
    """Defect 1: the mobile dialog's description field must survive create."""
    r = client.post(
        "/api/jobs",
        json={"title": "desc smoke", "description": "Broken spring, north door"},
    )
    assert r.status_code == 201, r.text[:300]
    body = r.json()
    assert body["description"] == "Broken spring, north door"
    # SQLite stores the Uuid PK as 32-char hex; normalize like the app does.
    row = _job_row(client, body["id"].replace("-", ""))
    assert row["description"] == "Broken spring, north door"


def test_create_job_unscheduled_derives_service_call_status(client: TestClient) -> None:
    """Defect 2: no scheduled_at + no explicit status → "Service Call",
    agreeing with lifecycle_stage — not the old phantom "Scheduled"."""
    r = client.post("/api/jobs", json={"title": "unscheduled smoke"})
    assert r.status_code == 201, r.text[:300]
    body = r.json()
    assert body["status"] == "Service Call"
    row = _job_row(client, body["id"].replace("-", ""))
    assert row["status"] == "Service Call"
    assert row["lifecycle_stage"] == "service_call"


def test_create_job_explicit_status_still_honored(client: TestClient) -> None:
    """Desktop sends an explicit status on some paths — it must win."""
    r = client.post(
        "/api/jobs", json={"title": "explicit status", "status": "In Progress"}
    )
    assert r.status_code == 201, r.text[:300]
    assert r.json()["status"] == "In Progress"


def test_create_job_stamps_created_by(client: TestClient) -> None:
    """Defect 3 prerequisite: the creating user's id lands on the row."""
    r = client.post("/api/jobs", json={"title": "created-by smoke"})
    assert r.status_code == 201, r.text[:300]
    row = _job_row(client, r.json()["id"].replace("-", ""))
    assert row["created_by"] == "user-creator"


# ─── Part B: creator visibility in the mobile router ───────────────────────

CREATOR = {"user_id": "user-creator", "role": "technician", "tenant_id": "tenant-a"}
OTHER_TECH_USER = {"user_id": "user-other", "role": "technician", "tenant_id": "tenant-a"}


def _as_json(response) -> dict:
    return json.loads(response.body)


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "mobile_visibility_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed(SessionLocal, *, creator_has_tech_row: bool = True, assigned_to: str | None = None) -> str:
    """One dialog-shaped job: created by user-creator, no schedule, and
    ``assigned_to`` as given (None = the fresh-from-dialog state)."""
    db = SessionLocal()
    now = datetime.now(UTC)
    job_id = f"job-{uuid4().hex[:8]}"
    try:
        if creator_has_tech_row:
            db.execute(
                text(
                    "INSERT INTO technicians (id, company_id, user_id, active, created_at) "
                    "VALUES ('tech-creator', 'tenant-a', 'user-creator', 1, :now)"
                ),
                {"now": now},
            )
        db.execute(
            text(
                "INSERT INTO technicians (id, company_id, user_id, active, created_at) "
                "VALUES ('tech-other', 'tenant-a', 'user-other', 1, :now)"
            ),
            {"now": now},
        )
        db.execute(
            text(
                "INSERT INTO jobs (id, company_id, title, description, dispatch_status, "
                "created_by, assigned_to, scheduled_at, created_at, deleted_at) "
                "VALUES (:id, 'tenant-a', 'Dialog job', 'from mobile', :ds, "
                "'user-creator', :assigned_to, NULL, :now, NULL)"
            ),
            {
                "id": job_id,
                "ds": "assigned" if assigned_to else "unassigned",
                "assigned_to": assigned_to,
                "now": now,
            },
        )
        db.commit()
        return job_id
    finally:
        db.close()


def _list_ids(SessionLocal, user) -> list[str]:
    db = SessionLocal()
    try:
        r = mobile_router.mobile_all_my_jobs(
            request=_request(), current_user=user, db=db
        )
        assert r.status_code == 200
        return [j["id"] for j in _as_json(r)["jobs"]]
    finally:
        db.close()


def test_creator_sees_unassigned_job_in_list(session_factory):
    job_id = _seed(session_factory)
    assert job_id in _list_ids(session_factory, CREATOR)


def test_creator_without_technician_row_sees_job(session_factory):
    """The old early-return made the Jobs tab permanently empty for any
    account with no technicians row (e.g. the owner phone-testing)."""
    job_id = _seed(session_factory, creator_has_tech_row=False)
    assert job_id in _list_ids(session_factory, CREATOR)


def test_other_tech_does_not_see_creators_unassigned_job(session_factory):
    job_id = _seed(session_factory)
    assert job_id not in _list_ids(session_factory, OTHER_TECH_USER)


def test_creator_visibility_expires_on_assignment(session_factory):
    """Once dispatch assigns the job to another tech, it leaves the
    creator's list (and shows in the assignee's) — the tab is "my work +
    my pending creations", not a permanent creation log."""
    job_id = _seed(session_factory, assigned_to="tech-other")
    assert job_id not in _list_ids(session_factory, CREATOR)
    assert job_id in _list_ids(session_factory, OTHER_TECH_USER)


def test_creator_can_open_detail_of_unassigned_job(session_factory):
    job_id = _seed(session_factory)
    db = session_factory()
    try:
        r = mobile_router.get_mobile_job_detail(
            job_id=job_id, request=_request(), current_user=CREATOR, db=db
        )
        assert r.status_code == 200
        assert _as_json(r)["job"]["id"] == job_id
    finally:
        db.close()


def test_creator_loses_detail_access_after_assignment(session_factory):
    job_id = _seed(session_factory, assigned_to="tech-other")
    db = session_factory()
    try:
        with pytest.raises(HTTPException) as exc:
            mobile_router.get_mobile_job_detail(
                job_id=job_id, request=_request(), current_user=CREATOR, db=db
            )
        assert exc.value.status_code == 404
    finally:
        db.close()


def test_creator_cannot_write_start_on_unassigned_job(session_factory):
    """THE /audit finding: creator visibility must be read-only. The write
    gate (_assert_job_access → job_belongs_to_user) must still 404 the
    creator on mutating endpoints like /start, even while unassigned."""
    job_id = _seed(session_factory)
    db = session_factory()
    try:
        # mobile_job_start gates via _job_belongs_to_user and RETURNS a 404
        # response (it doesn't raise) — assert on the response object.
        r = mobile_router.mobile_job_start(
            job_id=job_id, request=_request(), current_user=CREATOR, db=db
        )
        assert r.status_code == 404, (
            "creator must NOT be able to start their unassigned job — "
            f"got {r.status_code}"
        )
    finally:
        db.close()


def test_job_belongs_to_user_ignores_created_by(session_factory):
    """Belt-and-suspenders on the same finding, at the shared-helper level:
    created_by must never satisfy the WRITE ownership rule."""
    job_id = _seed(session_factory)
    db = session_factory()
    try:
        assert not job_belongs_to_user(db, "tenant-a", job_id, "user-creator")
    finally:
        db.close()
