"""#632: two Celery tasks that nothing could trigger are gone, and stay gone.

Both were registered on the worker with no beat entry, no caller and no
``send_task`` by name, the shape of ``tasks/email_poller.py`` in #599:

* ``check_estimate_followups`` (``tasks/estimate_followup.py``) stamped
  ``estimates.reminder_sent_at`` and logged "reminders_sent" without sending
  anything. Scheduling it would have faked a customer follow-up. Prod had 0
  stamped estimates (maintainer ruling, 2026-09-13).
* ``run_daily_snapshot_task`` (``core/celery_app.py``) was ``return None``, with
  its own task route and no caller. The pre-deletion re-run of #632's search
  found it.

Absence is asserted against the worker's registry, loaded the way the worker
loads it, not against source text. The control test proves the registry was
actually populated, so the absence checks cannot pass on an empty one.
"""
from __future__ import annotations

import importlib.util

import pytest

from gdx_dispatch.core.celery_app import celery_app

# The worker imports conf.include at startup; importing core.celery_app alone
# does not (see test_beat_schedule_registration.py).
celery_app.loader.import_default_modules()

RETIRED_TASK_NAMES = [
    "check_estimate_followups",
    "gdx_dispatch.core.celery_app.run_daily_snapshot_task",
]


def test_registry_is_loaded() -> None:
    """Control: a task that lives only in the include list is registered."""
    assert "billing_followup.daily_tick" in celery_app.tasks, (
        "the worker registry did not load, so the absence checks below prove nothing"
    )


@pytest.mark.parametrize("name", RETIRED_TASK_NAMES)
def test_retired_task_is_not_registered(name: str) -> None:
    assert name not in celery_app.tasks, f"{name} is registered on the worker again"


def test_estimate_followup_module_is_gone() -> None:
    assert importlib.util.find_spec("gdx_dispatch.tasks.estimate_followup") is None
    assert "gdx_dispatch.tasks.estimate_followup" not in celery_app.conf.include


def test_snapshot_task_route_is_gone() -> None:
    assert "gdx_dispatch.core.celery_app.run_daily_snapshot_task" not in (celery_app.conf.task_routes or {})
