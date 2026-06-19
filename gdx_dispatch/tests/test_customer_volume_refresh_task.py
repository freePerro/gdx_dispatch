"""Sprint 1.0.6 — nightly customer-volume refresh celery task.

The actual UPDATE SQL is Postgres-specific (UPDATE..FROM, INTERVAL); we
exercise it in Phase 3 against the real GDX database. These tests cover
the orchestrator (does it invoke the helper) and the registration
(is the task wired into celery + the beat schedule).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.scheduler import build_beat_schedule
from gdx_dispatch.tasks import customer_volume_refresh


@pytest.fixture(autouse=True)
def _celery_eager_mode():
    original_always_eager = celery_app.conf.task_always_eager
    original_eager_propagates = celery_app.conf.task_eager_propagates
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        celery_app.conf.task_always_eager = original_always_eager
        celery_app.conf.task_eager_propagates = original_eager_propagates


def test_task_registered_with_celery():
    assert "refresh_all_customer_rolling_volumes" in celery_app.tasks


def test_task_in_beat_schedule():
    schedule = build_beat_schedule()
    assert "refresh-customer-rolling-volumes-nightly" in schedule
    entry = schedule["refresh-customer-rolling-volumes-nightly"]
    assert entry["task"] == "refresh_all_customer_rolling_volumes"
    assert entry["options"] == {"queue": "priority:low"}


def test_orchestrator_calls_refresh_once():
    """Single-tenant: _refresh_tenant_volumes is called exactly once."""
    with patch.object(customer_volume_refresh, "_refresh_tenant_volumes", return_value=7) as mock_refresh:
        result = customer_volume_refresh.refresh_all_customer_rolling_volumes()

    assert result == {"tenants_checked": 1, "customers_updated": 7}
    assert mock_refresh.call_count == 1


def test_orchestrator_swallows_refresh_failure():
    """Refresh failure must NOT crash the celery worker — logs and returns zero."""
    with patch.object(
        customer_volume_refresh, "_refresh_tenant_volumes",
        side_effect=Exception("boom"),
    ):
        result = customer_volume_refresh.refresh_all_customer_rolling_volumes()
    assert result == {"tenants_checked": 1, "customers_updated": 0}
