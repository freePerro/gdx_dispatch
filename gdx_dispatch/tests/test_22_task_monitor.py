"""
Tests for the new Celery task monitoring endpoints added in task_monitor.py.

5 tests:
  1. test_task_stats_endpoint        — GET /api/admin/tasks/stats structure + types
  2. test_active_tasks_endpoint      — GET /api/admin/tasks/active returns list
  3. test_scheduled_tasks_endpoint   — GET /api/admin/tasks/scheduled returns beat schedule
  4. test_task_history               — get_task_history() returns [] gracefully when Redis is down
  5. test_task_monitor_page_renders  — GET /admin/tasks returns 200 HTML
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_token(monkeypatch):
    token = "test-admin-22"
    monkeypatch.setenv("ADMIN_API_TOKEN", token)
    import gdx_dispatch.core.task_monitor as tm
    monkeypatch.setattr(tm, "ADMIN_TOKEN", token)
    return token


@pytest.fixture()
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture()
def app_client(admin_token):
    """Minimal FastAPI app with only the task_monitor router."""
    from gdx_dispatch.core.task_monitor import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fake Celery app used to avoid needing a live broker
# ---------------------------------------------------------------------------


class _FakeBeatConf:
    beat_schedule = {
        "retry-failed-webhooks": {
            "task": "gdx_dispatch.core.webhooks.tasks.retry_failed_webhooks_task",
            "schedule": 60.0,
            "options": {"queue": "low"},
        },
        "weekly-schema-drift": {
            "task": "gdx_dispatch.core.reconciliation_tasks.weekly_schema_drift_task",
            "schedule": "crontab(day_of_week=0,hour=3,minute=0)",
            "options": {"queue": "low"},
        },
    }
    task_default_queue = "low"


class _FakeCeleryApp:
    conf = _FakeBeatConf()

    def send_task(self, name, **kw):
        # No-op: broker not required in unit tests
        return None


# ---------------------------------------------------------------------------
# Test 1 — /api/admin/tasks/stats structure and types
# ---------------------------------------------------------------------------


def test_task_stats_endpoint(app_client, auth_headers, monkeypatch):
    """Stats endpoint must return correct keys with numeric values."""
    import gdx_dispatch.core.task_monitor as tm

    # Stub out get_task_history so we don't hit Redis
    monkeypatch.setattr(tm, "get_task_history", lambda limit=500: [])
    # Stub inspect to avoid broker
    monkeypatch.setattr(tm, "_safe_inspect", lambda: None)

    r = app_client.get("/api/admin/tasks/stats", headers=auth_headers)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()

    assert "total_today" in data
    assert "failed_today" in data
    assert "avg_duration_ms" in data
    assert "queues" in data

    assert isinstance(data["total_today"], int)
    assert isinstance(data["failed_today"], int)
    assert isinstance(data["avg_duration_ms"], (int, float))
    assert isinstance(data["queues"], dict)

    # Must have high and low queue entries
    assert "high" in data["queues"]
    assert "low" in data["queues"]
    for qname in ("high", "low"):
        q = data["queues"][qname]
        assert "pending" in q
        assert "active" in q
        assert isinstance(q["pending"], int)
        assert isinstance(q["active"], int)


# ---------------------------------------------------------------------------
# Test 2 — /api/admin/tasks/active returns list
# ---------------------------------------------------------------------------


def test_active_tasks_endpoint(app_client, auth_headers, monkeypatch):
    """Active tasks endpoint returns a list (empty when broker unreachable)."""
    import gdx_dispatch.core.task_monitor as tm

    # Stub inspect to simulate no workers available
    monkeypatch.setattr(tm, "_safe_inspect", lambda: None)

    r = app_client.get("/api/admin/tasks/active", headers=auth_headers)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert isinstance(data, list)


def test_active_tasks_endpoint_with_data(app_client, auth_headers, monkeypatch):
    """Active tasks endpoint parses worker inspect data correctly."""
    import datetime

    import gdx_dispatch.core.task_monitor as tm

    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    class _MockInspect:
        def active(self):
            return {
                "worker1@host": [
                    {
                        "id": "abc-123",
                        "name": "gdx_dispatch.core.celery_app.run_daily_snapshot_task",
                        "time_start": now_ts - 5.0,
                        "args": [],
                        "kwargs": {"tenant_id": "tenant-xyz"},
                    }
                ]
            }
        def reserved(self):
            return {}

    monkeypatch.setattr(tm, "_safe_inspect", lambda: _MockInspect())

    r = app_client.get("/api/admin/tasks/active", headers=auth_headers)
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["task_id"] == "abc-123"
    assert t["tenant_id"] == "tenant-xyz"
    assert t["worker"] == "worker1@host"
    assert t["duration_s"] is not None
    assert t["duration_s"] >= 4.9


# ---------------------------------------------------------------------------
# Test 3 — /api/admin/tasks/scheduled returns beat schedule
# ---------------------------------------------------------------------------


def test_scheduled_tasks_endpoint(app_client, auth_headers, monkeypatch):
    """Scheduled tasks endpoint returns list with required keys from beat_schedule."""
    import gdx_dispatch.core.celery_app as ca

    fake = _FakeCeleryApp()
    monkeypatch.setattr(ca, "celery_app", fake)

    r = app_client.get("/api/admin/tasks/scheduled", headers=auth_headers)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    rows = r.json()
    assert isinstance(rows, list)
    # The existing beat schedule from celery_app has 3 entries
    assert len(rows) >= 1

    for row in rows:
        assert "name" in row
        assert "task" in row
        assert "schedule" in row or "schedule_human" in row
        assert "queue" in row


# ---------------------------------------------------------------------------
# Test 4 — get_task_history graceful degradation when Redis is down
# ---------------------------------------------------------------------------


def test_task_history(monkeypatch):
    """get_task_history must return [] (not raise) when Redis is unreachable."""
    # Point backend at a port that won't be listening
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:19998/9")

    from gdx_dispatch.core.task_monitor import get_task_history

    result = get_task_history(limit=10)
    assert isinstance(result, list)
    assert result == []


# ---------------------------------------------------------------------------
# Test 5 — GET /admin/tasks page renders HTML
# ---------------------------------------------------------------------------


def test_task_monitor_page_renders(admin_token, monkeypatch):
    """GET /admin/tasks must return 200 with HTML content."""
    from gdx_dispatch.app import create_app

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    # The /admin/tasks route does not require auth token — it's a UI page
    r = client.get("/admin/tasks")
    assert r.status_code == 200
    content_type = r.headers.get("content-type", "")
    assert "text/html" in content_type, f"Expected HTML, got: {content_type}"
    # Should contain key dashboard elements
    body = r.text
    assert "Task Monitor" in body or "task" in body.lower()
