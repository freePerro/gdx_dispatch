"""Tests for gdx_dispatch/routers/tasks.py — internal task/todo management."""
from __future__ import annotations

import logging
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.routers import tasks as tasks_router


def _make_client(tenant_id: str = "tenant-test", *, raise_server_exceptions: bool = True):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    _setup = SessionLocal()
    # replace inline DDL with metadata-based creation
    TenantBase.metadata.create_all(engine, checkfirst=True)
    _setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :cid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"g-{tenant_id}", "cid": tenant_id},
    )
    _setup.commit()
    _setup.close()

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(tasks_router.router)
    app.dependency_overrides[tasks_router.get_db] = _override_db
    app.dependency_overrides[tasks_router.get_current_user] = lambda: {
        "user_id": "test-user",
        "sub": "test-user",
        "email": "test@example.com",
        "role": "admin",
    }

    tc = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    return tc, SessionLocal, engine


@pytest.fixture()
def client():
    tc, SessionLocal, engine = _make_client()
    yield tc, SessionLocal
    tc.app.dependency_overrides.clear()
    engine.dispose()


def _payload(**overrides) -> dict:
    base = {
        "title": "Follow up with customer",
        "description": "Call about install schedule",
        "priority": "normal",
        "status": "open",
    }
    base.update(overrides)
    return base


def test_create_task(client):
    tc, _ = client
    r = tc.post("/api/tasks", json=_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert UUID(data["id"])
    assert data["title"] == "Follow up with customer"
    assert data["priority"] == "normal"
    assert data["status"] == "open"
    assert data["completed_at"] is None
    assert data["company_id"] == "tenant-test"


def test_list_tenant_scoped():
    tc_a, SessionA, engine_a = _make_client(tenant_id="tenant-a")
    tc_b, SessionB, engine_b = _make_client(tenant_id="tenant-b")

    ra = tc_a.post("/api/tasks", json=_payload(title="A-task"))
    assert ra.status_code == 201, ra.text
    rb = tc_b.post("/api/tasks", json=_payload(title="B-task"))
    assert rb.status_code == 201, rb.text

    list_a = tc_a.get("/api/tasks").json()
    list_b = tc_b.get("/api/tasks").json()
    assert [t["title"] for t in list_a] == ["A-task"]
    assert [t["title"] for t in list_b] == ["B-task"]

    tc_a.app.dependency_overrides.clear()
    tc_b.app.dependency_overrides.clear()
    engine_a.dispose()
    engine_b.dispose()


def test_filter_by_status(client):
    tc, _ = client
    t1 = tc.post("/api/tasks", json=_payload(title="open-1")).json()
    tc.post("/api/tasks", json=_payload(title="open-2"))
    done = tc.post("/api/tasks", json=_payload(title="done-1")).json()
    assert tc.post(f"/api/tasks/{done['id']}/complete").status_code == 200

    open_list = tc.get("/api/tasks", params={"status": "open"}).json()
    open_titles = {t["title"] for t in open_list}
    assert "open-1" in open_titles and "open-2" in open_titles
    assert "done-1" not in open_titles

    done_list = tc.get("/api/tasks", params={"status": "completed"}).json()
    done_titles = {t["title"] for t in done_list}
    assert done_titles == {"done-1"}
    assert t1["id"] in {t["id"] for t in open_list}


def test_complete_updates_timestamp(client):
    tc, _ = client
    created = tc.post("/api/tasks", json=_payload()).json()
    assert created["completed_at"] is None

    r = tc.post(f"/api/tasks/{created['id']}/complete")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


def test_reopen_clears_completed_at(client):
    tc, _ = client
    created = tc.post("/api/tasks", json=_payload()).json()
    tc.post(f"/api/tasks/{created['id']}/complete")

    r = tc.post(f"/api/tasks/{created['id']}/reopen")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "open"
    assert data["completed_at"] is None


def test_soft_delete(client):
    tc, SessionLocal = client
    created = tc.post("/api/tasks", json=_payload(title="delete-me")).json()

    r = tc.delete(f"/api/tasks/{created['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    assert tc.get(f"/api/tasks/{created['id']}").status_code == 404
    listed = tc.get("/api/tasks").json()
    assert all(t["id"] != created["id"] for t in listed)

    db = SessionLocal()
    try:
        row = db.execute(
            select(tasks_router.Task).where(tasks_router.Task.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# A refused audit write must not take the request down with it (GDXA-57/GDXA-44)
# ---------------------------------------------------------------------------
#
# All five mutating handlers commit their change and THEN audit. That ordering is
# correct — but it means a failed audit write arrives after the change is already
# durable, so a 500 is a lie: the work happened and the user is told it did not.
# Worse, the failed flush deactivates the session, and every one of the five reads
# an ORM attribute back through it to build the response, so the handler raises
# PendingRollbackError on a task it has already written.
#
# Before the swap onto `audit_best_effort` these returned 500 with the row saved,
# and the user's natural response — retry — made a second task. Measured, not
# argued: the tests below fail on the pre-swap code at the status assertion.
#
# The failure is injected at the STORAGE layer, never by patching the audit
# function in Python. The defect is what a failed *flush* does to the session, and
# only a real statement failure produces that; a Python-level raise would leave
# the session healthy and these tests would pass against still-broken code.


def _refuse_audit_inserts(engine) -> None:
    """Make audit_logs refuse every INSERT, from the database itself."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER audit_logs_refuse_insert
                BEFORE INSERT ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'audit storage refuses this row');
                END;
                """
            )
        )


def _audit_count(SessionLocal, action: str) -> int:
    db = SessionLocal()
    try:
        return db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        ).scalar_one()
    finally:
        db.close()


def _task_row(SessionLocal, task_id: str):
    db = SessionLocal()
    try:
        return db.execute(
            select(tasks_router.Task).where(tasks_router.Task.id == UUID(task_id))
        ).scalar_one_or_none()
    finally:
        db.close()


# (id, action, expected status, request, assertion that the change is durable)
_MUTATIONS = [
    (
        "create",
        "task_created",
        201,
        lambda tc, tid: tc.post("/api/tasks", json=_payload(title="audit-refused")),
        lambda row: row.title == "audit-refused",
    ),
    (
        "update",
        "task_updated",
        200,
        lambda tc, tid: tc.patch(f"/api/tasks/{tid}", json={"title": "renamed"}),
        lambda row: row.title == "renamed",
    ),
    (
        "complete",
        "task_completed",
        200,
        lambda tc, tid: tc.post(f"/api/tasks/{tid}/complete"),
        lambda row: row.status == "completed" and row.completed_at is not None,
    ),
    (
        "reopen",
        "task_reopened",
        200,
        lambda tc, tid: tc.post(f"/api/tasks/{tid}/reopen"),
        lambda row: row.status == "open" and row.completed_at is None,
    ),
    (
        "delete",
        "task_deleted",
        200,
        lambda tc, tid: tc.delete(f"/api/tasks/{tid}"),
        lambda row: row.deleted_at is not None,
    ),
]


@pytest.mark.parametrize(
    "name,action,expected_status,do_request,change_is_durable",
    _MUTATIONS,
    ids=[m[0] for m in _MUTATIONS],
)
def test_mutation_survives_a_refused_audit_write(
    name, action, expected_status, do_request, change_is_durable, caplog
):
    # raise_server_exceptions=False so the test reads the status code the browser
    # would get instead of the exception TestClient would otherwise re-raise.
    tc, SessionLocal, engine = _make_client(raise_server_exceptions=False)
    try:
        # Seed with auditing HEALTHY, so the only refused write is the one under
        # test. Nothing to seed for create — the request under test is the create.
        task_id = None
        if name != "create":
            seeded = tc.post("/api/tasks", json=_payload(title="seeded"))
            assert seeded.status_code == 201, seeded.text
            task_id = seeded.json()["id"]

        _refuse_audit_inserts(engine)

        with caplog.at_level(logging.ERROR):
            r = do_request(tc, task_id)

        # (b) the response is the success the user earned, not a 500.
        assert r.status_code == expected_status, r.text

        # (a) the change survived — it was already committed before the audit ran.
        row = _task_row(SessionLocal, task_id or r.json()["id"])
        assert row is not None
        assert change_is_durable(row)

        # The trail really is lost. That is the accepted trade for this half of
        # the pair: after the fact no rollback can undo the change, so refusing
        # the response would only make the user repeat it.
        assert _audit_count(SessionLocal, action) == 0

        # (c) ...which is why the ERROR log is the only record, and must exist.
        # `audit_best_effort` names it `audit_best_effort_failed`; this asserts the
        # level and that the action is identifiable, not the exact prefix.
        failures = [
            rec for rec in caplog.records
            if rec.levelno >= logging.ERROR and action in rec.getMessage()
        ]
        assert failures, (
            f"a refused audit write for {action} left no ERROR record; the change "
            f"is durable and now nothing at all knows it happened"
        )
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_task_routes_still_audit_when_storage_is_healthy():
    """The control arm. Without it, a 'fix' that simply stopped auditing would
    turn every assertion above green."""
    tc, SessionLocal, engine = _make_client()
    try:
        created = tc.post("/api/tasks", json=_payload(title="audited"))
        assert created.status_code == 201, created.text
        tid = created.json()["id"]

        assert tc.patch(f"/api/tasks/{tid}", json={"title": "audited-2"}).status_code == 200
        assert tc.post(f"/api/tasks/{tid}/complete").status_code == 200
        assert tc.post(f"/api/tasks/{tid}/reopen").status_code == 200
        assert tc.delete(f"/api/tasks/{tid}").status_code == 200

        for action in (
            "task_created",
            "task_updated",
            "task_completed",
            "task_reopened",
            "task_deleted",
        ):
            assert _audit_count(SessionLocal, action) == 1, f"missing audit row: {action}"
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()
