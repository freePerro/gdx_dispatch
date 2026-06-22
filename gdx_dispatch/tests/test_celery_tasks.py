from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.tasks import recurring, reminders


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


def test_reminder_sends_sms(monkeypatch):
    appointment_id = str(uuid4())
    now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    start_at = now + timedelta(hours=24)

    sent: list[tuple[str, str]] = []

    monkeypatch.setattr(reminders, "_now_utc", lambda: now)
    monkeypatch.setattr(
        reminders,
        "_get_appointment",
        lambda _appointment_id: {
            "id": appointment_id,
            "start_at": start_at,
            "customer_phone": "+15551234567",
        },
    )
    monkeypatch.setattr(
        reminders,
        "_send_sms",
        lambda phone, message: sent.append((phone, message)),
    )

    result = reminders.send_appointment_reminder.delay(appointment_id).get()

    assert result["status"] == "sent"
    assert sent
    assert sent[0][0] == "+15551234567"


def test_recurring_job_created(monkeypatch):
    from unittest.mock import MagicMock
    tenant_id = str(uuid4())

    mock_db = MagicMock()

    # Phase C: recurring.py uses SessionLocal() directly (no per-tenant session factory).
    monkeypatch.setattr(recurring, "SessionLocal", lambda: mock_db)
    monkeypatch.setattr(
        recurring,
        "materialize_due_recurring_jobs",
        lambda db, actor_id, tenant_id: {"created_count": 1},
    )

    result = recurring.generate_recurring_jobs.delay(tenant_id).get()

    assert result["created_count"] == 1


def test_s122_3_qb_sync_stub_removed():
    """S122-3 (T2): the no-op qb_sync stub was deleted 2026-05-12. It was
    wired to celery beat (every 15 min) and produced synced_count=0 forever
    because _pull_qb_data/_push_qb_data were no-ops and _list_tenant_ids
    returned []. Real periodic sync arrives via the CDC poller in Phase 2;
    webhooks (CloudEvents-aware per S122-CE) carry the active path until then.
    """
    import importlib
    import pytest
    with pytest.raises(ImportError):
        importlib.import_module("gdx_dispatch.tasks.qb_sync")
