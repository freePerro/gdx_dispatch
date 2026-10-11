"""The Jinja onboarding wizard is retired, and so is the module that held it.

`core/onboarding.py` used to export a second router, `ui_router`, serving an
HTML wizard at `GET /onboarding`, `GET /onboarding/{step}` and
`POST /onboarding/{step}` off `templates/onboarding.html`. Nothing ever mounted
it: `app.py` imported the object and never called `include_router` on it, so no
request could reach any of those three routes or render that page. The Vue SPA's
`/onboarding` route (`OnboardingView.vue`) owns the surface. Deleted under
GDXA-24, on the maintainer's ruling on GDXA-19; same shape as the
`integrations.html` / `ui_router` removal recorded in
`test_33_integrations.py::test_integrations_page_renders`.

GDXA-24 left the module's JSON half, `GET /api/onboarding`, mounted because its
brief required it, and this file pinned that route. It had no caller anywhere:
not `frontend/src`, not the mobile routers, not the MCP tools. It was a parallel
implementation of onboarding over Redis/process memory with six steps of its
own, so it could not describe the wizard the app really drives, which is
`gdx_dispatch/routers/onboarding.py` (`/api/onboarding/state|step|complete|
checklist`, DB-backed). GDXA-579 deleted the module, its mount and its tests,
and turned the presence half of this file into the absence assertion below.

Falsifier, so a reader can check these guards can fail: restore
`core/onboarding.py`, and `test_core_onboarding_module_is_gone` goes red;
mount any router at `GET /api/onboarding` and
`test_bare_api_onboarding_is_not_mounted` goes red.

`templates/onboarding.html` itself is owned by documents-media and was deleted
separately, under GDXA-25; `test_the_wizard_template_is_gone_from_the_tree`
holds that here. The two halves are independent on purpose: an unrendered
template cannot answer a request, so the load-bearing assertion is that no code
renders it (`test_nothing_renders_the_jinja_onboarding_page`), and the tree
assertion only stops the orphan coming back.
"""
from __future__ import annotations

import importlib.util
import pathlib

from gdx_dispatch.tests.conftest import iter_app_routes


def _routes() -> list[tuple[str, object]]:
    from gdx_dispatch.app import create_app

    return list(iter_app_routes(create_app()))


# ── The dead implementation must stay dead ───────────────────────────────────


def test_core_onboarding_module_is_gone():
    """The Redis-backed parallel onboarding, deleted under GDXA-579."""
    root = pathlib.Path(__file__).resolve().parents[1]
    assert not (root / "core" / "onboarding.py").exists(), (
        "core/onboarding.py is back. routers/onboarding.py is the onboarding "
        "API OnboardingView.vue drives; a second, Redis-backed model of the "
        "same wizard cannot describe it."
    )
    assert importlib.util.find_spec("gdx_dispatch.core.onboarding") is None


def test_bare_api_onboarding_is_not_mounted():
    """`GET /api/onboarding` was served only by the deleted module."""
    paths = {path for path, _route in _routes()}
    assert "/api/onboarding" not in paths, (
        "GET /api/onboarding is mounted again. It had no caller; the wizard's "
        "onboarding API is routers/onboarding.py."
    )


def test_no_server_rendered_onboarding_html_routes_are_mounted():
    """No `/onboarding` or `/onboarding/{step}` route in the live table.

    The SPA catch-all serves those paths as client-side routes; it registers
    under its own wildcard path, not under `/onboarding`.
    """
    dead = sorted(
        path
        for path, _route in _routes()
        if path == "/onboarding" or path.startswith("/onboarding/")
    )
    assert dead == [], f"a server-rendered onboarding route is back: {dead}"


def test_nothing_renders_the_jinja_onboarding_page():
    """No non-test source file names the template as a string literal.

    This is the assertion that keeps the page unreachable regardless of whether
    the (separately owned) template file has been removed from the tree yet.

    It looks for the *quoted* name, which is the shape a render call has to
    take (``templates.TemplateResponse(request, "onboarding.html", ...)``), not
    the bare substring — prose that merely names the retired page, including
    the docstring of this file, is not a
    renderer. Known hole, same one the repo's other link guards carry: a
    template name handed over in a variable is invisible here. The route-table
    assertions above are what make the page unreachable either way.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if rel.startswith("tests/") or rel.startswith("frontend/"):
            continue
        body = py.read_text(encoding="utf-8", errors="ignore")
        if '"onboarding.html"' in body or "'onboarding.html'" in body:
            offenders.append(rel)
    assert offenders == [], f"something renders the retired wizard page: {offenders}"


def test_the_wizard_template_is_gone_from_the_tree():
    """The orphan file itself, deleted under GDXA-25 once nothing rendered it.

    This could not ship with GDXA-24 — the template was still on disk then, so
    the assertion would have been red on the commit that introduced it.

    It is the weaker half of the pair and says so: a template that no code
    names is already unreachable, which is
    `test_nothing_renders_the_jinja_onboarding_page`'s job. What this adds is
    that the 21KB of dead markup cannot quietly reappear and start collecting
    dead links again — `href="/settings/stripe-connect"` at its line 266, a
    CTA for a capability retired 2026-09-01, is what surfaced the whole thread.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    assert not (root / "templates" / "onboarding.html").exists(), (
        "templates/onboarding.html is back. Nothing renders it (see the "
        "assertion above), so it can only be dead markup — delete it rather "
        "than wiring it up; the Vue SPA's OnboardingView.vue owns this surface."
    )
