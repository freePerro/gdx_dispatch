"""The Jinja onboarding wizard is retired (2026-09-24) — and `/api/onboarding` is not.

`core/onboarding.py` used to export a second router, `ui_router`, serving an
HTML wizard at `GET /onboarding`, `GET /onboarding/{step}` and
`POST /onboarding/{step}` off `templates/onboarding.html`. Nothing ever mounted
it: `app.py` imported the object and never called `include_router` on it, so no
request could reach any of those three routes or render that page. The Vue SPA's
`/onboarding` route (`OnboardingView.vue`) owns the surface. Deleted under
GDXA-24, on the maintainer's ruling on GDXA-19; same shape as the
`integrations.html` / `ui_router` removal recorded in
`test_33_integrations.py::test_integrations_page_renders`.

**The second half of this file is the reason it exists.** The deletion had a
trap in it. `app.py` imported both routers inside ONE `try` with ONE shared
`except`:

    try:
        from gdx_dispatch.core.onboarding import router as core_onboarding_router
        from gdx_dispatch.core.onboarding import ui_router as onboarding_ui_router
    except Exception:
        ...
        core_onboarding_router = APIRouter(tags=["onboarding"])

Removing `ui_router` from the module while leaving that second import standing
makes it raise `ImportError`, the shared `except` fires, and
`core_onboarding_router` — the JSON router that IS mounted — is rebound to an
empty `APIRouter`. `app.include_router(core_onboarding_router, prefix="/api")`
then mounts nothing and `GET /api/onboarding` silently disappears. The app still
boots and `/health` still passes; the only signal is one logged exception at
startup. So it gets a test that can only be green when the route is really there,
rather than one that proves the app imports.

**What these presence tests do NOT claim.** They hold that the route stays
mounted because GDXA-24's brief required exactly that. They are not a finding
that the route is used: grep finds no caller of bare `GET /api/onboarding` in
`frontend/src`, the mobile routers or the MCP tools. The onboarding surface the
app really drives is `gdx_dispatch/routers/onboarding.py`
(`/api/onboarding/state|step|complete|checklist`), which `OnboardingView.vue`
calls. `core/onboarding.py` is a parallel implementation of the same idea over
Redis/process memory with a different six steps. Whether it should survive at all
is an open question for the maintainer — if the answer is no, delete this file's
presence half with it rather than working around it.

Nor do they claim the handler is *correct*: `get_onboarding_api` wraps its body
in `except Exception` and returns HTTP 200 with `complete: 0` and
`next_step: "company_info"`, so a Redis outage reads as "fresh install" rather
than as an error. Pre-existing, and these tests cannot fail for it — they assert
the route is mounted and whose handler it is, not what it returns under fault.

Falsifier, stated so a future reader can check this guard can actually fail:
rebind `core_onboarding_router` to `APIRouter()` in `app.py` and
`test_api_onboarding_route_is_still_mounted` and
`test_api_onboarding_is_served_by_the_live_handler` both go red. The route-table
drift in `openapi_routes.txt` would also catch it, but it reads as "you changed
the route list", not "you unmounted a live API".

`templates/onboarding.html` itself is owned by documents-media and was deleted
separately, under GDXA-25; `test_the_wizard_template_is_gone_from_the_tree`
holds that here. The two halves are independent on purpose: an unrendered
template cannot answer a request, so the load-bearing assertion is that no code
renders it (`test_nothing_renders_the_jinja_onboarding_page`), and the tree
assertion only stops the orphan coming back.
"""
from __future__ import annotations

import pathlib

from gdx_dispatch.tests.conftest import iter_app_routes

# ── The live surface must survive the deletion ────────────────────────────────


def _routes() -> list[tuple[str, object]]:
    from gdx_dispatch.app import create_app

    return list(iter_app_routes(create_app()))


def test_api_onboarding_route_is_still_mounted():
    """Trap 2: a shared `except` in app.py can empty the live router silently."""
    paths = {path for path, _route in _routes()}
    assert "/api/onboarding" in paths, (
        "GET /api/onboarding is gone from the mounted route table. The likely "
        "cause is the onboarding import block in app.py falling into its "
        "`except` and rebinding core_onboarding_router to an empty APIRouter — "
        "read the startup log for 'Failed to import router: "
        "core_onboarding_router'."
    )


def test_api_onboarding_is_served_by_the_live_handler():
    """Path presence alone could come from any router; pin the handler.

    An empty fallback ``APIRouter`` has no endpoint at all, so this fails for
    the same trap even if some other router later claims the same path.
    """
    matches = [route for path, route in _routes() if path == "/api/onboarding"]
    assert matches, "no route object registered at /api/onboarding"
    endpoints = {getattr(r, "endpoint", None).__name__ for r in matches if getattr(r, "endpoint", None)}
    assert "get_onboarding_api" in endpoints, (
        f"/api/onboarding is mounted but not by core.onboarding.get_onboarding_api: {endpoints}"
    )


def test_onboarding_state_functions_are_still_exported():
    """The four functions `/api/onboarding` is built on, and the wizard is not."""
    from gdx_dispatch.core import onboarding

    for name in (
        "get_onboarding_status",
        "complete_step",
        "get_next_step",
        "is_onboarding_complete",
    ):
        assert callable(getattr(onboarding, name, None)), f"{name} is gone"


# ── The dead wizard must stay dead ───────────────────────────────────────────


def test_ui_router_is_gone_from_core_onboarding():
    from gdx_dispatch.core import onboarding

    assert not hasattr(onboarding, "ui_router"), (
        "core.onboarding.ui_router is back. It was never mounted; if it is "
        "wanted again it needs its own import block in app.py, not a second "
        "import sharing core_onboarding_router's `except`."
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
    the docstring in ``core/onboarding.py`` recording this deletion, is not a
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


def test_wizard_form_validators_are_gone():
    """The form-validation half of the wizard, which only the POST route called."""
    from gdx_dispatch.core import onboarding

    for name in (
        "WizardFormData",
        "WizardForm",
        "_validate_step",
        "_validate_company_info",
        "_step_template_ctx",
        "_wizard_form",
        "templates",
    ):
        assert not hasattr(onboarding, name), f"wizard residue is back: {name}"
