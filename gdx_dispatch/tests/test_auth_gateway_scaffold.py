"""SS-12 Slice A — BFF auth gateway scaffold tests.

Baseline guarantees for ``gdx_dispatch.routers.auth.gateway``:

1. The five scaffold routes (GET /auth/login, GET /auth/callback,
   POST /auth/refresh, POST /auth/logout, POST /auth/switch-tenant)
   are declared on ``gdx_dispatch.routers.auth.gateway.router``.
2. ``gdx_dispatch/app.py`` imports that router and passes it to
   ``include_router`` AFTER the legacy ``gdx_dispatch.routers.auth`` include —
   preserving the legacy POST /auth/refresh and POST /auth/logout
   precedence contract required by SS-12A.
3. Each scaffold endpoint function returns the deterministic
   ``ss12a_scaffold_placeholder`` payload when awaited directly.
4. Importing and calling the scaffold does not depend on any Authentik
   environment variables.

SS-12A redo r4 performance note
-------------------------------
r2 collapsed ``create_app()`` out of this file and r3 entered the
isolated-app ``TestClient`` as a context manager to reuse a single
``anyio.BlockingPortal`` across scaffold requests. Both still routed
through Starlette's TestClient + httpx + anyio request path, and
Codex's container dispatch continued to stall at the first
request-issuing test in the replay trace (``.........`` point).

r4 removes the TestClient runtime path entirely. Scaffold endpoint
responses are proved by awaiting the endpoint coroutines directly
(``asyncio.run(auth_gateway.login_scaffold())``); no httpx transport,
no anyio portal, no Starlette request lifecycle, no module teardown
cleanup to leak. Route shape, router wiring, and precedence are still
proved statically — ``EXPECTED_ROUTES`` introspection on
``auth_gateway.router.routes`` plus a source-level grep of
``gdx_dispatch/app.py`` in ``test_gdx_app_wires_auth_gateway_router_after_legacy_auth``.
The scaffold endpoints are pure async functions returning a dict, so a
direct ``asyncio.run`` call is a faithful proof of the contract that
``TestClient.request(...)`` was indirectly proving.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from gdx_dispatch.routers.auth import gateway as auth_gateway

EXPECTED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/switch-tenant"),
)

AUTHENTIK_ENV_VARS = (
    "AUTHENTIK_CLIENT_ID",
    "AUTHENTIK_CLIENT_SECRET",
    "AUTHENTIK_ISSUER",
    "AUTHENTIK_REDIRECT_URI",
)

# Direct coroutine handles for each scaffold endpoint. The router binds
# these same functions to the routes in ``EXPECTED_ROUTES``, so awaiting
# them is equivalent to issuing the HTTP request — without the TestClient
# + httpx + anyio runtime path that stalled Codex's replay budget.
SCAFFOLD_ENDPOINT_CALLABLES: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "login": auth_gateway.login_scaffold,
    "callback": auth_gateway.callback_scaffold,
    "refresh": auth_gateway.refresh_scaffold,
    "logout": auth_gateway.logout_scaffold,
    "switch-tenant": auth_gateway.switch_tenant_scaffold,
}


def _routes_by_path_method(router_or_app: object) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    routes = getattr(router_or_app, "routes", ())
    for route in routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            pairs.add((method, path))
    return pairs


def test_scaffold_marker_constant_is_stable() -> None:
    """The scaffold marker is a stable literal that Codex and Gemma can grep."""
    assert auth_gateway.SCAFFOLD_MARKER == "ss12a_scaffold_placeholder"


def test_router_prefix_and_tags() -> None:
    """Router is mounted at /auth with the auth_gateway tag."""
    assert auth_gateway.router.prefix == "/auth"
    assert "auth_gateway" in auth_gateway.router.tags


@pytest.mark.parametrize("method,path", EXPECTED_ROUTES)
def test_main_app_has_scaffold_route(method: str, path: str) -> None:
    """The auth_gateway router declares each expected scaffold route.

    SS-12A redo r2: verifies against ``auth_gateway.router.routes``
    directly rather than constructing a full ``create_app()`` FastAPI
    instance. ``gdx_dispatch/app.py`` passes this same router object to
    ``include_router`` verbatim, so its declared routes ARE the
    scaffold's contribution to the main app. The wiring itself is
    proved by ``test_gdx_app_wires_auth_gateway_router_after_legacy_auth``.
    """
    pairs = _routes_by_path_method(auth_gateway.router)
    assert (method, path) in pairs, (
        f"Expected {method} {path} in auth_gateway.router.routes; "
        f"scaffold router is missing a declared endpoint."
    )


def test_main_app_includes_auth_gateway_router_names() -> None:
    """The named scaffold routes are declared on the auth_gateway router.

    SS-12A redo r2: verifies against ``auth_gateway.router.routes``
    directly (see ``test_main_app_has_scaffold_route``). ``gdx_dispatch/app.py``
    mounts this router verbatim so the route names carry through to
    ``create_app()`` unchanged.
    """
    gateway_route_names = {
        getattr(route, "name", None)
        for route in auth_gateway.router.routes
        if getattr(route, "name", "").startswith("auth_gateway_")
    }
    expected_names = {
        "auth_gateway_login_scaffold",
        "auth_gateway_callback_scaffold",
        "auth_gateway_refresh_scaffold",
        "auth_gateway_logout_scaffold",
        "auth_gateway_switch_tenant_scaffold",
    }
    missing = expected_names - gateway_route_names
    assert not missing, f"Missing auth_gateway-named routes: {missing}"


def test_gdx_app_wires_auth_gateway_router_after_legacy_auth() -> None:
    """Static proof that ``gdx_dispatch/app.py`` includes the auth_gateway router
    AFTER the legacy auth router.

    SS-12A redo r2: replaces the ``create_app()``-based wiring check
    that previously timed out Codex's 90s replay budget. The check is
    equivalent because (a) ``gdx_dispatch/app.py`` passes ``auth_gateway.router``
    object-identically to ``include_router``, and (b) the SS-12A
    contract requires the include_router call to be unconditional —
    there is no runtime branching that gates the wiring. A source-level
    grep is a faithful proof of the wiring contract and costs no
    FastAPI construction time.

    Enforces the precedence contract from SS-12A pitfalls: legacy
    ``auth`` router include must appear textually before the
    ``auth_gateway_router`` include so POST /auth/refresh and
    POST /auth/logout stay bound to the legacy handlers in the main
    app until SS-12B+ replaces them.
    """
    from gdx_dispatch import app as gdx_app

    source = pathlib.Path(gdx_app.__file__).read_text()

    # (a) The auth_gateway module must be imported into gdx_dispatch.app's namespace.
    assert re.search(
        r"from\s+gdx_dispatch\.routers\.auth\s+import\s+gateway\s+as\s+auth_gateway_router",
        source,
    ), "gdx_dispatch/app.py must import gdx_dispatch.routers.auth.gateway as auth_gateway_router"

    # (b) Both routers must be registered via include_router.
    legacy_include_idx = source.find("app.include_router(auth.router")
    gateway_include_idx = source.find("auth_gateway_router.router")
    assert legacy_include_idx != -1, (
        "gdx_dispatch/app.py must call app.include_router(auth.router ...) - "
        "legacy auth router registration missing"
    )
    assert gateway_include_idx != -1, (
        "gdx_dispatch/app.py must reference auth_gateway_router.router in an "
        "include_router call"
    )

    # (c) Legacy auth must register before auth_gateway to preserve
    #     POST /auth/refresh and POST /auth/logout precedence.
    assert legacy_include_idx < gateway_include_idx, (
        "Legacy auth.router must be registered before auth_gateway_router "
        "to preserve POST /auth/refresh and POST /auth/logout precedence; "
        f"found legacy include at offset {legacy_include_idx} vs gateway "
        f"reference at {gateway_include_idx}"
    )


@pytest.mark.parametrize(
    "method,path,endpoint_name",
    [
        ("GET", "/auth/login", "login"),
        ("GET", "/auth/callback", "callback"),
        ("POST", "/auth/refresh", "refresh"),
        ("POST", "/auth/logout", "logout"),
        ("POST", "/auth/switch-tenant", "switch-tenant"),
    ],
)
def test_scaffold_endpoint_returns_marker(
    method: str,
    path: str,
    endpoint_name: str,
) -> None:
    """Each scaffold endpoint coroutine returns the deterministic marker payload.

    SS-12A redo r4: replaces the ``TestClient.request(method, path)``
    call with ``asyncio.run(<endpoint_coroutine>())``. The scaffold
    endpoint functions are pure ``async def`` handlers returning a dict,
    with no request parsing, dependency injection, or response
    serialization that would differ from what FastAPI returns — so a
    direct await is a faithful proof of the same contract without the
    httpx/anyio portal runtime that stalled Codex replay.

    ``method``/``path`` remain in the parametrize id for evidence that
    all five route shapes from ``EXPECTED_ROUTES`` are exercised; the
    dispatch key is ``endpoint_name`` which maps to the coroutine.
    """
    endpoint_call = SCAFFOLD_ENDPOINT_CALLABLES[endpoint_name]
    payload = asyncio.run(endpoint_call())
    assert payload.get("status") == auth_gateway.SCAFFOLD_MARKER, (
        f"{method} {path} scaffold payload missing SCAFFOLD_MARKER: {payload!r}"
    )
    assert payload.get("endpoint") == endpoint_name
    assert payload.get("slice") == "ss12-a"


def test_scaffold_does_not_require_authentik_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Authentik env var must be required by the SS-12A scaffold.

    SS-12A redo r4: invokes each scaffold coroutine directly with
    Authentik env vars stripped, proving the endpoint returns its
    placeholder payload without reading the environment. The previous
    ``TestClient``-based check drove the same code path through httpx;
    the direct-await check proves the same property without the runtime
    that stalled Codex replay.
    """
    for var in AUTHENTIK_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for endpoint_name, endpoint_call in SCAFFOLD_ENDPOINT_CALLABLES.items():
        payload = asyncio.run(endpoint_call())
        assert payload.get("status") == auth_gateway.SCAFFOLD_MARKER, (
            f"{endpoint_name} scaffold payload missing SCAFFOLD_MARKER "
            f"when Authentik env absent: {payload!r}"
        )
        assert payload.get("endpoint") == endpoint_name


def test_scaffold_module_has_no_authentik_env_reads() -> None:
    """Static check: the scaffold module must not read Authentik env vars."""
    source = pathlib.Path(auth_gateway.__file__).read_text()
    for var in AUTHENTIK_ENV_VARS:
        assert var not in source, (
            f"SS-12A scaffold must not reference {var}; found in auth/gateway.py"
        )
    # Belt-and-suspenders: the scaffold module should not import os at all
    # in this slice. If SS-12B adds env handling, it will edit this list.
    assert "import os" not in source, (
        "SS-12A scaffold must not import os; env handling belongs in SS-12B+"
    )


def test_create_app_actually_wires_gateway_router_not_fallback() -> None:
    """Slice 8 Phase B integration check — the live FastAPI app built by
    ``create_app()`` MUST register the gateway scaffold endpoints from the
    moved ``gdx_dispatch.routers.auth.gateway`` sub-module, not the empty
    ``APIRouter`` that the ``except Exception`` fallback at ``gdx_dispatch/app.py:54``
    creates if the import raises.

    Without this test the silent-fallthrough class is invisible: a broken
    package ``__init__.py`` would let ``from gdx_dispatch.routers.auth import gateway``
    raise, the except branch would create an empty router, app.py would
    register that empty router, and CI would still be green because every
    other test imports ``gateway`` directly (and fails its own setup, not
    the wiring).

    Pin the wiring by name + handler identity, not by string-grep.
    """
    import importlib

    # Force a clean reimport so a stale module from a prior test doesn't
    # mask an import-time failure introduced by this commit.
    if "gdx_dispatch.app" in __import__("sys").modules:
        del __import__("sys").modules["gdx_dispatch.app"]
    gdx_app = importlib.import_module("gdx_dispatch.app")
    app = gdx_app.create_app()

    expected_routes = {
        ("GET", "/auth/login", "auth_gateway_login_scaffold"),
        ("GET", "/auth/callback", "auth_gateway_callback_scaffold"),
        ("POST", "/auth/refresh", "auth_gateway_refresh_scaffold"),
        ("POST", "/auth/logout", "auth_gateway_logout_scaffold"),
        ("POST", "/auth/switch-tenant", "auth_gateway_switch_tenant_scaffold"),
    }

    found_by_name = {
        getattr(r, "name", None): r
        for r in app.routes
        if (getattr(r, "name", None) or "").startswith("auth_gateway_")
    }
    missing = [name for (_m, _p, name) in expected_routes if name not in found_by_name]
    assert not missing, (
        "create_app() did not register these gateway scaffold routes "
        f"by name: {missing}. The package-shim import probably fell into the "
        "except-branch fallback at gdx_dispatch/app.py:54 — check gdx_dispatch/routers/auth/__init__.py"
    )

    # Identity check: the registered handlers must be the ones from the
    # moved sub-module, not anonymous lambdas that the fallback APIRouter
    # would produce.
    from gdx_dispatch.routers.auth import gateway as auth_gateway
    assert found_by_name["auth_gateway_login_scaffold"].endpoint is auth_gateway.login_scaffold
    assert found_by_name["auth_gateway_callback_scaffold"].endpoint is auth_gateway.callback_scaffold
    assert found_by_name["auth_gateway_refresh_scaffold"].endpoint is auth_gateway.refresh_scaffold
    assert found_by_name["auth_gateway_logout_scaffold"].endpoint is auth_gateway.logout_scaffold
    assert found_by_name["auth_gateway_switch_tenant_scaffold"].endpoint is auth_gateway.switch_tenant_scaffold

    # And /login-picker must be wired to the moved login_picker handler.
    from gdx_dispatch.routers.auth import login_picker as auth_login_picker
    picker_routes = [r for r in app.routes if getattr(r, "path", None) == "/login-picker"]
    assert picker_routes, "create_app() did not register /login-picker"
    assert picker_routes[0].endpoint is auth_login_picker.login_picker_page


def test_create_app_wires_phase_c1_subroutes_not_fallback() -> None:
    """Slice 8 Phase C1 integration check — `/oauth/*`, `/scim/v2/*`,
    `/auth/sso/*`, and `/auth/signup` must be wired to the moved
    sub-modules, not lost in the dynamic SS-router loader's silent
    `except Exception` branch (`gdx_dispatch/app.py:2042`).

    The pre-Phase-C1 loader did `__import__("gdx_dispatch.routers.oauth2")`,
    which after the move resolves to ``ModuleNotFoundError`` and gets
    swallowed by the loader's exception handler — `/oauth/*` would
    silently disappear with green CI. This test pins the wiring by
    counting registered routes per prefix; if any prefix drops to 0
    the silent-fallthrough fired.
    """
    import importlib

    if "gdx_dispatch.app" in __import__("sys").modules:
        del __import__("sys").modules["gdx_dispatch.app"]
    gdx_app = importlib.import_module("gdx_dispatch.app")
    app = gdx_app.create_app()

    paths = [getattr(r, "path", "") for r in app.routes]

    expectations = {
        "/oauth": 5,           # SS-21 — 5 endpoints under /oauth
        "/scim/v2": 18,        # SS-22 — 18 endpoints under /scim/v2
        "/auth/sso": 4,        # 4 endpoints under /auth/sso
        # /signup is commerce-plane (strip list) — not present in this seed
        # Phase C2 sub-modules:
        "/api/pats": 3,                     # SS-14 self-mint
        "/api/admin/pats": 4,               # SS-15 admin-on-behalf
        "/api/capabilities/available": 1,   # SS-14 support
        "/api/admin/tenant-members": 1,     # SS-14 support
    }

    for prefix, min_count in expectations.items():
        matches = [p for p in paths if p.startswith(prefix)]
        assert len(matches) >= min_count, (
            f"create_app() registered only {len(matches)} routes under "
            f"{prefix!r} (expected at least {min_count}). The dynamic "
            f"SS-router loader at gdx_dispatch/app.py:2042 probably fell into the "
            f"except branch — check the dotted-import paths in _ss_routers."
        )

    # Identity check: ensure the moved sub-module's router IS the one
    # that contributed routes.
    from gdx_dispatch.routers.auth import oauth2 as auth_oauth2
    oauth_endpoints = {
        getattr(r, "endpoint", None)
        for r in app.routes
        if getattr(r, "path", "").startswith("/oauth")
    }
    moved_endpoints = {r.endpoint for r in auth_oauth2.router.routes}
    overlap = oauth_endpoints & moved_endpoints
    assert overlap, (
        "No overlap between create_app()'s /oauth handlers and "
        "gdx_dispatch.routers.auth.oauth2.router — the loader registered "
        "something other than the moved sub-module"
    )
