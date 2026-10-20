"""GDXA-1946: a return-visit child records which path made it, and a closeout
continuation is not labelled a callback.

All three writers of a parent-linked child set ``is_return_visit`` True, so
``get_job`` labelled every closeout's "we have to come back" child CALLBACK
(red tag, "different P&L treatment") the moment the parent completed. The
child now carries ``return_visit_source``; ``get_job`` skips ``closeout``.
``callback`` (spawn-return-visit, "Add warranty / callback") and
``follow_up`` keep the label, as does NULL (a row with no creation audit row
for 135 to backfill from), which is how every child read before (Doug,
2026-10-19).

Each test drives the real endpoint that creates the child, then reads it
back through ``GET /api/jobs/{id}``.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router

TENANT_ID = "00000000-0000-4000-8000-0000000001a6"
USER = "user-1"
ROOT = pathlib.Path(__file__).resolve().parents[1]


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
        request.state.tenant_id = TENANT_ID
        request.state.request_id = "gdxa-1946"
        return await call_next(request)

    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER, "sub": USER, "role": "admin", "tenant_id": TENANT_ID,
    }
    yield TestClient(app, raise_server_exceptions=True), Session
    engine.dispose()


def _job(Session, **kw) -> str:
    db = Session()
    try:
        job = Job(id=uuid4(), company_id=TENANT_ID, title=kw.pop("title", "job"),
                  customer_id=uuid4(), **kw)
        db.add(job)
        db.commit()
        return str(job.id)
    finally:
        db.close()


def _source(Session, job_id: str) -> str | None:
    db = Session()
    try:
        return db.scalar(select(Job.return_visit_source).where(Job.id == UUID(job_id)))
    finally:
        db.close()


def _completed_parent(Session) -> str:
    return _job(Session, title="parent", lifecycle_stage="completed", status="Completed",
                completed_at=datetime.now(UTC) - timedelta(days=3), assigned_to=USER)


def test_closeout_continuation_is_not_a_callback(harness):
    tc, Session = harness
    parent = _job(Session, title="Two-day install", lifecycle_stage="in_progress",
                  dispatch_status="on_site", assigned_to=USER)
    r = tc.post(f"/api/jobs/{parent}/closeout", json={
        "parts": [], "hours": 6.0, "no_parts_used": True,
        "needs_return_visit": True, "return_visit_reason": "Day 2: hang the second door.",
    })
    assert r.status_code == 201, r.text[:400]
    child = r.json()["return_visit_job_id"]
    assert _source(Session, child) == "closeout"

    body = tc.get(f"/api/jobs/{child}").json()
    assert body["is_return_visit"] is True
    assert body["return_visit_source"] == "closeout"
    assert body["is_callback"] is False
    assert body["callback_undetermined"] is False


def test_spawned_warranty_callback_is_a_callback(harness):
    tc, Session = harness
    parent = _completed_parent(Session)
    r = tc.post(f"/api/jobs/{parent}/spawn-return-visit",
                json={"reason": "opener reversing again", "scheduled_at": datetime.now(UTC).isoformat()})
    assert r.status_code == 201, r.text[:400]
    child = r.json()["id"]
    assert _source(Session, child) == "callback"
    assert tc.get(f"/api/jobs/{child}").json()["is_callback"] is True


def test_follow_up_still_counts_as_a_callback(harness):
    """Unchanged by GDXA-1946: whether a follow-up is a callback is open."""
    tc, Session = harness
    parent = _completed_parent(Session)
    r = tc.post(f"/api/jobs/{parent}/follow-up", json={})
    assert r.status_code == 201, r.text[:400]
    child = r.json()["id"]
    assert _source(Session, child) == "follow_up"
    assert tc.get(f"/api/jobs/{child}").json()["is_callback"] is True


@pytest.mark.parametrize("is_return_visit", [True, False])
def test_child_without_a_source_reads_as_before(harness, is_return_visit):
    """No creation audit row for 135 to backfill from: NULL, still a callback."""
    tc, Session = harness
    parent = _completed_parent(Session)
    child = _job(Session, title="legacy", lifecycle_stage="scheduled", parent_job_id=UUID(parent),
                 is_return_visit=is_return_visit, scheduled_at=datetime.now(UTC))
    assert tc.get(f"/api/jobs/{child}").json()["is_callback"] is True


def test_closeout_child_of_an_undated_parent_is_not_undetermined(harness):
    """CALLBACK? is a question about a callback; a continuation never asks it."""
    tc, Session = harness
    parent = _job(Session, title="parent", lifecycle_stage="completed", status=None)
    child = _job(Session, title="child", lifecycle_stage="service_call", parent_job_id=UUID(parent),
                 is_return_visit=True, return_visit_source="closeout")
    body = tc.get(f"/api/jobs/{child}").json()
    assert body["is_callback"] is False
    assert body["callback_undetermined"] is False


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def test_every_return_visit_writer_names_its_source():
    """The class guard: a ``Job(...)`` built with ``is_return_visit=True`` must
    say which path made it, or get_job cannot tell a continuation from a
    callback again."""
    missing: list[str] = []
    found = 0
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "migrations/")) or "/node_modules/" in rel:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Job"):
                continue
            flag = _kw(node, "is_return_visit")
            if flag is None or (isinstance(flag, ast.Constant) and flag.value is False):
                continue
            found += 1
            if _kw(node, "return_visit_source") is None:
                missing.append(f"{rel}:{node.lineno}")
    assert found >= 3, "the guard no longer sees the three writers in routers/jobs.py"
    assert missing == [], missing
