from __future__ import annotations

from uuid import uuid4

import pytest

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.tasks import recurring


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


def test_appointment_reminders_stub_removed():
    """The appointment-reminder task was a no-op wired to celery beat.

    It fired hourly on prod and logged `succeeded ... {'scheduled_count': 0}`
    every time: _find_upcoming_appointment_ids returned [], _get_appointment
    returned None, _send_sms did nothing. The test that used to live here
    monkeypatched all three, so it proved the stub could call itself and
    nothing else — a green test over a feature that had never sent a message.

    Removed 2026-08-22, following the qb_sync and late_fees precedent. The
    blocker is transport, not the finder: every SMS path funnels through
    core/sms.py (Twilio) and no SMS credentials are set on prod at all, so
    wiring it would have sent zero messages while logging success. Re-add with
    the task when an outbound transport exists.
    """
    import importlib

    import pytest

    with pytest.raises(ImportError):
        importlib.import_module("gdx_dispatch.tasks.reminders")


def test_every_beat_entry_names_a_task_the_worker_has_registered():
    """The 2026-07-07 prod audit found a beat entry naming a task that never
    existed — every firing died as "unregistered task", silently, for months.

    Two things an adversarial review caught in the first version of this test,
    both of which would have let that recur:

    1. It checked ``hasattr(module, attr)``. celery_app.py's own comment records
       the incident as "defined but never imported by the worker" — a module can
       define the function and the WORKER still not know it. Registration in
       ``celery_app.tasks`` is the property that actually matters.
    2. It skipped any task not starting with "gdx_dispatch.", which is 19 of the
       29 entries — the outlook.*, phone_com.*, invoice_reminders.* and
       billing_followup.* namespaces, i.e. most of the schedule.

    Both fixed: every entry, checked against the real registry.
    """
    from gdx_dispatch.core.celery_app import celery_app
    from gdx_dispatch.core.scheduler import build_beat_schedule

    # Do what a worker does at startup: import everything in `include`/`imports`.
    # Without this the registry holds only what this test process happened to
    # import, and four legitimately-registered tasks (verified against the live
    # gdx-celery-beat-1 worker) look missing — a false alarm that would get the
    # whole test deleted the first time it fired.
    celery_app.loader.import_default_modules()

    schedule = build_beat_schedule()
    assert len(schedule) > 20, f"only {len(schedule)} beat entries — schedule did not load"

    unregistered = [
        f"{name} -> {entry.get('task')}"
        for name, entry in schedule.items()
        if str(entry.get("task", "")) not in celery_app.tasks
    ]
    assert not unregistered, (
        "beat schedule points at tasks the worker has not registered "
        "(they will fire and die as 'unregistered task'):\n" + "\n".join(unregistered)
    )


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
