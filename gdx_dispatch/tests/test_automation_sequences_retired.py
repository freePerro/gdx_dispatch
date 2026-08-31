"""The "Automations" sequences shell is retired (2026-08-31) and must stay so.

`routers/automations.py` created sequences / steps / enrollments that nothing
ever executed — no task, no beat entry, no reader of `AutomationStep.action_type`.
Its UI toasted "Automation created" over a no-op. The engine that runs is
`modules/workflows` (`/api/workflows`), gated by the same `automations` module
key. See docs/design/unimplemented-endpoints-decision-list.md (2026-08-31).

These are absence assertions on the live route table (not source-text
presence): a re-added router shows up here whatever file it lives in.
"""
from __future__ import annotations

import pathlib

from gdx_dispatch.tests.conftest import iter_app_routes


def _paths() -> set[str]:
    from gdx_dispatch.app import create_app

    return {path for path, _route in iter_app_routes(create_app())}


def test_no_api_automations_route_survives():
    dead = sorted(p for p in _paths() if p.startswith("/api/automations"))
    assert dead == [], f"sequences shell is back: {dead}"


def test_event_rules_engine_still_registered():
    paths = _paths()
    assert "/api/workflows" in paths, "the engine the redirect lands on must exist"


def test_app_no_longer_wires_the_router():
    app_py = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    source = app_py.read_text(encoding="utf-8")
    assert "routers import automations" not in source
    assert "automations_router" not in source
    assert not (pathlib.Path(__file__).resolve().parents[1] / "routers" / "automations.py").exists()
