"""Every beat entry must resolve to a task the workers actually register.

2026-07-07 prod audit: four scheduled workflows were silently dead because
beat fired task names no worker had registered —

* ``qb_sync_schedule_dispatcher`` — quickbooks.tasks was never in the
  celery ``include`` list (KeyError in celery-low every 5 minutes).
* ``generate_recurring_jobs_for_all_tenants`` — beat pointed at a task
  name that never existed (real task: ``generate_recurring_jobs``).
* ``tech_locations_prune`` / ``timeclock_sweep`` — bound to the vestigial
  Sprint-1 ``gdx_dispatch/celery_app.py`` app instance nothing ran
  (66-day open shift, 65-day-old GPS rows in prod).

Beat publishes by NAME with no registry check, and the worker error is a
log line nobody watches — so a schedule/registry mismatch ships silently.
These tests make the mismatch a CI failure instead.
"""
from __future__ import annotations

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.scheduler import build_beat_schedule

# The worker imports conf.include at STARTUP — merely importing
# core.celery_app does not (billing_followup / invoice_reminders_auto
# live only in the include list, not the module-bottom imports). Load
# them the same way the worker does so we assert against the worker's
# real registration surface.
celery_app.loader.import_default_modules()


def test_every_beat_task_is_registered():
    registered = set(celery_app.tasks.keys())
    missing = {
        entry_name: spec["task"]
        for entry_name, spec in build_beat_schedule().items()
        if spec["task"] not in registered
    }
    assert not missing, (
        "beat schedule entries point at unregistered tasks (beat will fire "
        f"them into the void every interval, forever): {missing}"
    )


def test_every_beat_queue_is_consumed():
    consumed = {q.name for q in celery_app.conf.task_queues}
    bad = {
        entry_name: spec["options"]["queue"]
        for entry_name, spec in build_beat_schedule().items()
        if spec.get("options", {}).get("queue") not in consumed
    }
    assert not bad, (
        f"beat entries route to queues no worker consumes {sorted(consumed)}: {bad}"
    )


def test_every_task_routes_to_a_consumed_queue():
    """A task routed to a queue no worker consumes vanishes on .delay().

    Caught in the same audit: every quickbooks task declared
    ``queue="low"``, timeclock_sweep declared ``queue="priority.low"``,
    and webhooks/campaigns/reconciliation carried pre-rename
    ``"high"``/``"low"`` decorator queues that silently OVERRODE their
    task_routes entries — all publishing to queues the workers never
    read. Resolve each task through the real router (decorator queue >
    task_routes > default) and require the destination to be consumed.
    """
    consumed = {q.name for q in celery_app.conf.task_queues}
    bad = {}
    for name, task in sorted(celery_app.tasks.items()):
        if name.startswith("celery."):  # celery-internal housekeeping tasks
            continue
        options = {"queue": task.queue} if getattr(task, "queue", None) else {}
        route = celery_app.amqp.router.route(options, name)
        queue = route.get("queue")
        queue_name = getattr(queue, "name", queue)
        if queue_name not in consumed:
            bad[name] = queue_name
    assert not bad, (
        f"tasks route to queues outside the consumed set {sorted(consumed)}: {bad}"
    )
