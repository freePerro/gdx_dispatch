"""Campaigns, retired 2026-09-14 (#638, #636), stay retired.

Maintainer rulings 2026-09-10 and 2026-09-13: retire `modules/campaigns/` (its
service, Celery task and models had no entry point once its router left), the
Campaigns tab, and `routers/campaigns.py`, whose `POST /{id}/send` set
`status='sending'`, bumped `sent_count` and wrote an audit row while nothing
delivered anything. The SPA called 4 of its 11 routes; none was the send.
`mobile_sync_actions` goes with them: its only reader and writer, `POST
/api/mobile/sync`, left in #633. Migration 095 drops the three tables (0 rows on
prod and demo, 2026-09-13 and 2026-09-14). No audit row for any campaign action
exists on prod.

Kept by the ruling: `MarketingCampaign` / `marketing_campaigns` (prod holds one
soft-deleted QA seed row). Winback's campaigns are a separate surface and stay.

Absence is asserted against the mounted route table and the worker's registry,
not against source text; the ORM registry (fresh interpreter) is checked in
test_migration_095_drop_campaigns_mobile_sync.py. The SPA's
redirects are pinned in frontend/src/router/__tests__/campaignsRetired.spec.js.
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections import Counter
from pathlib import Path

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
    "gdx_dispatch.modules.campaigns",
    "gdx_dispatch.routers.campaigns",
])
def test_module_no_longer_exists(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None, f"{dotted} is back"


REMOVED_PATHS = [
    ("GET", "/api/campaigns"),
    ("POST", "/api/campaigns"),
    ("GET", "/api/campaigns/{campaign_id}"),
    ("PATCH", "/api/campaigns/{campaign_id}"),
    ("DELETE", "/api/campaigns/{campaign_id}"),
    ("POST", "/api/campaigns/{campaign_id}/send"),
    ("PUT", "/api/campaigns/{campaign_id}/activate"),
    ("PUT", "/api/campaigns/{campaign_id}/deactivate"),
    ("GET", "/api/campaigns/{campaign_id}/preview"),
    ("GET", "/api/campaigns/{campaign_id}/sends"),
    ("POST", "/api/campaigns/preview-filter"),
]


@pytest.mark.parametrize(("method", "path"), REMOVED_PATHS)
def test_retired_route_is_not_registered(registrations: Counter, method: str, path: str) -> None:
    assert registrations[(method, path)] == 0, f"{method} {path} is registered again"


def test_neighbours_are_still_served(registrations: Counter) -> None:
    """Winback's campaigns share the word and nothing else; Segments is where
    the old Campaigns links now land. A retirement that took either with it
    would pass every absence check above."""
    for method, path in [
        ("GET", "/api/winback/campaigns"),
        ("POST", "/api/winback/campaigns"),
        ("POST", "/api/winback/campaigns/{campaign_id}/send"),
        ("GET", "/api/segments"),
        ("GET", "/api/loyalty/tiers"),
        ("GET", "/api/workflows"),
    ]:
        assert registrations[(method, path)] >= 1, f"{method} {path} stopped being served"


def test_the_module_key_is_retired() -> None:
    from gdx_dispatch.core.modules import LEGACY_MODULE_ALIASES, MODULES

    assert "campaigns" not in MODULES
    assert "campaigns" not in LEGACY_MODULE_ALIASES
    assert "campaigns" not in LEGACY_MODULE_ALIASES.values()


def test_the_campaign_task_is_off_the_worker() -> None:
    from gdx_dispatch.core.celery_app import celery_app

    celery_app.loader.import_default_modules()  # what the worker loads at startup
    assert "billing_followup.daily_tick" in celery_app.tasks, "control: the worker registry did not load"
    assert "gdx_dispatch.modules.campaigns.tasks.send_campaign_task" not in celery_app.tasks
    assert "gdx_dispatch.modules.campaigns.tasks" not in celery_app.conf.include
    assert "gdx_dispatch.modules.campaigns.tasks.*" not in (celery_app.conf.task_routes or {})


def test_the_spa_no_longer_routes_or_links_the_page() -> None:
    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    assert not (src / "views" / "CampaignsView.vue").exists()
    router = (src / "router" / "index.js").read_text(encoding="utf-8")
    nav = (src / "constants" / "modules.js").read_text(encoding="utf-8")
    assert "views/CampaignsView.vue" not in router
    assert not re.search(r"""to:\s*['"`]/campaigns['"`]""", nav), "a nav entry links /campaigns again"
