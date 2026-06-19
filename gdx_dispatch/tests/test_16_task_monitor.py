"""
Tests for gdx_dispatch/core/task_monitor.py — Celery task monitoring dashboard.

6 tests:
  1. metrics structure (keys + types)
  2. failed tasks list endpoint
  3. retry endpoint (queues task, updates status)
  4. queue depth check (get_queue_depth returns int, 0 on bad URL)
  5. scheduled tasks list
  6. admin-only access (403 without token)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_control_db():
    """In-memory SQLite control plane DB with task_executions table."""
    from gdx_dispatch.core.task_monitor import ControlBase

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(engine)
    return engine


def _make_app_with_monitor():
    """Create a minimal FastAPI app with the task monitor router mounted."""
    from fastapi import FastAPI

    from gdx_dispatch.core.task_monitor import router

    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_token(monkeypatch):
    """Set ADMIN_API_TOKEN for the duration of the test."""
    token = "test-admin-token-xyz"
    monkeypatch.setenv("ADMIN_API_TOKEN", token)
    # Patch the module-level variable too (already imported)
    import gdx_dispatch.core.task_monitor as tm
    monkeypatch.setattr(tm, "ADMIN_TOKEN", token)
    return token


@pytest.fixture()
def control_engine(monkeypatch):
    """Patch SessionLocal to use an in-memory SQLite DB."""
    engine = _make_control_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import gdx_dispatch.core.database as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(admin_token, control_engine):
    app = _make_app_with_monitor()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------

def _seed_tasks(engine, tasks: list[dict]):
    """Insert TaskExecution rows directly into the in-memory DB."""
    from datetime import datetime

    from gdx_dispatch.core.task_monitor import TaskExecution

    Session = sessionmaker(bind=engine)
    db = Session()
    for t in tasks:
        record = TaskExecution(
            task_name=t.get("task_name", "test.task"),
            task_id=t.get("task_id", "tid-" + t.get("task_name", "x")),
            status=t.get("status", "success"),
            started_at=t.get("started_at", datetime(2026, 1, 1, 12, 0, 0)),
            completed_at=t.get("completed_at"),
            duration_ms=t.get("duration_ms"),
            error_message=t.get("error_message"),
            retries=t.get("retries", 0),
            tenant_id=t.get("tenant_id"),
            args_summary=t.get("args_summary"),
        )
        db.add(record)
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Test 1 — metrics structure
# ---------------------------------------------------------------------------

def test_metrics_structure(client, auth_headers, control_engine):
    """GET /api/admin/tasks/metrics must return correct keys with numeric values."""
    from datetime import UTC, datetime

    _seed_tasks(control_engine, [
        {"task_name": "gdx_dispatch.task.a", "task_id": "t1", "status": "success",
         "started_at": datetime.now(UTC), "duration_ms": 120},
        {"task_name": "gdx_dispatch.task.b", "task_id": "t2", "status": "failure",
         "started_at": datetime.now(UTC)},
        {"task_name": "gdx_dispatch.task.c", "task_id": "t3", "status": "success",
         "started_at": datetime.now(UTC), "duration_ms": 80},
    ])

    r = client.get("/api/admin/tasks/metrics", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()

    required_keys = {"success_rate_24h", "avg_duration_ms", "failed_count",
                     "queue_depth_high", "queue_depth_low"}
    assert required_keys.issubset(data.keys()), f"Missing keys: {required_keys - data.keys()}"

    assert isinstance(data["success_rate_24h"], (int, float))
    assert isinstance(data["avg_duration_ms"], (int, float))
    assert isinstance(data["failed_count"], int)
    assert isinstance(data["queue_depth_high"], int)
    assert isinstance(data["queue_depth_low"], int)

    assert 0.0 <= data["success_rate_24h"] <= 100.0
    assert data["failed_count"] == 1
    # 2 successes out of 3 total → ~66.67%
    assert data["success_rate_24h"] == pytest.approx(66.67, abs=0.5)


# ---------------------------------------------------------------------------
# Test 2 — failed tasks list
# ---------------------------------------------------------------------------

def test_failed_tasks_list(client, auth_headers, control_engine):
    """GET /api/admin/tasks/failed must return only failure rows."""
    from datetime import UTC, datetime

    _seed_tasks(control_engine, [
        {"task_name": "job.a", "task_id": "f1", "status": "failure",
         "error_message": "timeout", "started_at": datetime.now(UTC)},
        {"task_name": "job.b", "task_id": "f2", "status": "success",
         "started_at": datetime.now(UTC)},
        {"task_name": "job.c", "task_id": "f3", "status": "failure",
         "error_message": "connection refused", "started_at": datetime.now(UTC)},
    ])

    r = client.get("/api/admin/tasks/failed", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "failure"
    task_names = {row["task_name"] for row in rows}
    assert "job.a" in task_names
    assert "job.c" in task_names
    # success row must not appear
    assert "job.b" not in task_names


# ---------------------------------------------------------------------------
# Test 3 — retry endpoint
# ---------------------------------------------------------------------------

def test_retry_endpoint(client, auth_headers, control_engine, monkeypatch):
    """POST /api/admin/tasks/{task_id}/retry must re-queue and set status=pending."""
    from datetime import UTC, datetime

    _seed_tasks(control_engine, [
        {"task_name": "job.retryable", "task_id": "retry-001",
         "status": "failure", "error_message": "boom",
         "started_at": datetime.now(UTC)},
    ])

    # Patch celery_app.send_task so we don't need a broker
    sent = []
    import gdx_dispatch.core.task_monitor as tm

    class _FakeApp:
        def send_task(self, name, **kw):
            sent.append(name)

    monkeypatch.setattr(tm, "celery_app", _FakeApp(), raising=False)

    # Also patch the import inside the route function
    import gdx_dispatch.core.celery_app as ca
    monkeypatch.setattr(ca, "celery_app", _FakeApp())

    r = client.post("/api/admin/tasks/retry-001/retry", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] is True
    assert body["task_id"] == "retry-001"

    # Verify status updated in DB
    from sqlalchemy.orm import sessionmaker as sm
    Session = sm(bind=control_engine)
    db = Session()
    from gdx_dispatch.core.task_monitor import TaskExecution
    record = db.query(TaskExecution).filter_by(task_id="retry-001").first()
    db.close()
    assert record is not None
    assert record.status == "pending"


def test_retry_endpoint_404(client, auth_headers):
    """POST /api/admin/tasks/{task_id}/retry must 404 for unknown task_id."""
    r = client.post("/api/admin/tasks/nonexistent-task-id/retry", headers=auth_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Test 4 — queue depth
# ---------------------------------------------------------------------------

def test_queue_depth_returns_int_on_bad_url(monkeypatch):
    """get_queue_depth must return 0 (not raise) when Redis is unreachable."""
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://127.0.0.1:19999/99")
    from gdx_dispatch.core.task_monitor import get_queue_depth

    result = get_queue_depth("high")
    assert isinstance(result, int)
    assert result == 0


# ---------------------------------------------------------------------------
# Test 5 — scheduled tasks list
# ---------------------------------------------------------------------------

def test_scheduled_tasks_list(client, auth_headers, monkeypatch):
    """GET /api/admin/tasks/scheduled must return list with name/task/schedule/queue keys."""
    import gdx_dispatch.core.task_monitor as tm

    class _FakeConf:
        beat_schedule = {
            "retry-webhooks": {
                "task": "gdx_dispatch.core.webhooks.tasks.retry_failed_webhooks_task",
                "schedule": 60.0,
                "options": {"queue": "low"},
            },
            "monthly-billing": {
                "task": "gdx_dispatch.core.reconciliation_tasks.monthly_billing_reconciliation_task",
                "schedule": "crontab(day_of_month=1)",
                "options": {"queue": "low"},
            },
        }

    class _FakeApp:
        conf = _FakeConf()
        def send_task(self, name, **kw):
            pass

    monkeypatch.setattr(tm, "celery_app", _FakeApp(), raising=False)

    import gdx_dispatch.core.celery_app as ca
    monkeypatch.setattr(ca, "celery_app", _FakeApp())

    r = client.get("/api/admin/tasks/scheduled", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 2
    for row in rows:
        assert "name" in row
        assert "task" in row
        assert "schedule" in row
        assert "queue" in row
    names = {row["name"] for row in rows}
    assert "retry-webhooks" in names
    assert "monthly-billing" in names


# ---------------------------------------------------------------------------
# Test 6 — admin-only access
# ---------------------------------------------------------------------------

def test_admin_only_access_denied():
    """All task monitor endpoints must return 403 without valid admin token."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import gdx_dispatch.core.task_monitor as tm

    # Ensure ADMIN_TOKEN is set so the guard is active (not 503)
    original = tm.ADMIN_TOKEN
    tm.ADMIN_TOKEN = "secret-prod-token"
    try:
        app = FastAPI()
        app.include_router(tm.router)
        c = TestClient(app, raise_server_exceptions=False)

        endpoints = [
            ("GET", "/api/admin/tasks/recent"),
            ("GET", "/api/admin/tasks/failed"),
            ("GET", "/api/admin/tasks/metrics"),
            ("GET", "/api/admin/tasks/scheduled"),
        ]
        for method, path in endpoints:
            r = c.request(method, path, headers={"Authorization": "Bearer wrong-token"})
            assert r.status_code == 403, (
                f"{method} {path} should return 403 with wrong token, got {r.status_code}"
            )
    finally:
        tm.ADMIN_TOKEN = original
