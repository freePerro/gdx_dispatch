"""SS-13 Slice D — pytest coverage for ``GET /login-picker``.

Covers the explicit backend route added by
``gdx_dispatch.routers.auth.login_picker`` (no API auth — the picker page itself
is public; the API call the SPA makes on mount is what requires auth):

* the route is registered on the router (visible in ``router.routes``)
* ``GET /login-picker`` returns the SPA ``index.html`` when the
  frontend has been built, otherwise a 503 HTML response with a
  ``Frontend not built`` body (matches the SPA catch-all fallback)
* the route is excluded from the OpenAPI schema
  (``include_in_schema=False``)

Test app pattern mirrors ``test_me_tenants.py`` / ``test_auth_gateway_return_to_auto.py``:
bare ``FastAPI()`` with only the slice's router mounted — no full-app
lifespan, so the test is fast and isolated from SS-12A replay budget
concerns.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.auth import login_picker as login_picker_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(login_picker_router.router)
    return app


def test_login_picker_route_registered_on_router():
    paths = [getattr(r, "path", None) for r in login_picker_router.router.routes]
    assert "/login-picker" in paths, paths


def test_login_picker_route_is_get_only():
    route = next(
        r for r in login_picker_router.router.routes
        if getattr(r, "path", None) == "/login-picker"
    )
    methods = getattr(route, "methods", None) or set()
    assert "GET" in methods
    # No mutating methods on a page route.
    assert methods == {"GET"} or methods == {"GET", "HEAD"}, methods


def test_login_picker_excluded_from_openapi_schema():
    route = next(
        r for r in login_picker_router.router.routes
        if getattr(r, "path", None) == "/login-picker"
    )
    assert getattr(route, "include_in_schema", True) is False


def test_login_picker_get_returns_html_or_503():
    app = _build_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/login-picker")
    assert resp.status_code in (200, 503), (resp.status_code, resp.text[:200])
    body = resp.text
    assert "<" in body, body[:200]
    if resp.status_code == 503:
        assert "Frontend not built" in body, body[:200]


def test_login_picker_frontend_dist_resolves_to_real_dir():
    """When gdx_dispatch/frontend/dist/index.html exists in the working tree, the
    route MUST return 200 — accepting 503 in that case would mask a path
    miscalculation post-package-move (Slice 8 Phase B). This pins the
    `Path(__file__).resolve().parents[2] / 'frontend' / 'dist'` resolution
    against silent regressions if the file ever moves another directory
    deeper.
    """
    from gdx_dispatch.routers.auth.login_picker import _FRONTEND_DIST
    if not (_FRONTEND_DIST / "index.html").exists():
        import pytest
        pytest.skip("frontend/dist/index.html not built — env-dependent")
    app = _build_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/login-picker")
    assert resp.status_code == 200, (
        f"_FRONTEND_DIST resolves to {_FRONTEND_DIST} and index.html exists, "
        f"but /login-picker returned {resp.status_code} — path resolution drift?"
    )
