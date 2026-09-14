"""Job Templates and Recurring Jobs, retired 2026-09-14 (#683), stay retired.

Owner ruling on #683. Neither feature ever worked for a user:
- the Job Templates page posted fields `JobTemplateCreateIn` did not accept, so
  every create was a 422;
- a recurring schedule needs a template, and the customer-page dialog disabled
  Save when none existed.
Prod (read-only, 2026-09-14): 0 `job_templates`, 0 `recurring_job_schedules`,
0 jobs with `source = 'template'`, and no audit row for either feature.

Absence is asserted against the mounted route table and the Celery beat
schedule, not against source text. The models are deliberately KEPT: migration
042 updates `job_templates`, and a fresh install builds that table from the ORM.
"""

from __future__ import annotations

import importlib.util
import os
from collections import Counter

import pytest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")


@pytest.fixture(scope="module")
def registrations() -> Counter:
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    counts: Counter = Counter()
    for full_path, route in iter_app_routes(app):
        for method in getattr(route, "methods", None) or ():
            counts[(method, full_path)] += 1
    return counts


@pytest.mark.parametrize("dotted", [
    "gdx_dispatch.routers.job_templates",
    "gdx_dispatch.routers.recurring_jobs",
    "gdx_dispatch.tasks.recurring",
])
def test_module_no_longer_exists(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None, f"{dotted} is back"


REMOVED_PATHS = [
    ("GET", "/api/job-templates"),
    ("POST", "/api/job-templates"),
    ("GET", "/api/job-templates/{template_id}"),
    ("PATCH", "/api/job-templates/{template_id}"),
    ("DELETE", "/api/job-templates/{template_id}"),
    ("POST", "/api/job-templates/{template_id}/apply"),
    ("GET", "/api/recurring"),
    ("POST", "/api/recurring"),
    ("PATCH", "/api/recurring/{schedule_id}"),
    ("DELETE", "/api/recurring/{schedule_id}"),
    ("GET", "/api/customers/{customer_id}/recurring-jobs"),
    ("POST", "/api/recurring-schedules/{schedule_id}/generate"),
]


@pytest.mark.parametrize(("method", "path"), REMOVED_PATHS)
def test_retired_route_is_not_registered(registrations: Counter, method: str, path: str) -> None:
    assert registrations[(method, path)] == 0, f"{method} {path} is registered again"


def test_neighbours_that_share_a_router_file_are_still_served(registrations: Counter) -> None:
    """The recurring GET and the generate route lived in files that serve other,
    live routes. A deletion that took the whole file would pass the absence
    checks above and silently drop these."""
    for method, path in [
        ("POST", "/api/customers/{customer_id}/optout"),  # routers/sub_resources.py
        ("GET", "/api/jobs/{job_id}/line-items"),  # routers/sub_resources.py
        ("GET", "/api/calendar/week"),  # routers/scheduling.py
    ]:
        assert registrations[(method, path)] >= 1, f"{method} {path} stopped being served"


def test_the_daily_generator_is_off_the_beat_schedule() -> None:
    from gdx_dispatch.core.celery_app import celery_app

    tasks = {entry.get("task") for entry in (celery_app.conf.beat_schedule or {}).values()}
    assert "gdx_dispatch.tasks.recurring.generate_recurring_jobs" not in tasks
    assert "gdx_dispatch.tasks.recurring" not in (celery_app.conf.include or ())


def test_the_spa_no_longer_routes_or_links_the_page() -> None:
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    assert not (src / "views" / "JobTemplatesView.vue").exists()
    router = (src / "router" / "index.js").read_text(encoding="utf-8")
    nav = (src / "constants" / "modules.js").read_text(encoding="utf-8")
    assert "/job-templates" not in router
    assert "/job-templates" not in nav
