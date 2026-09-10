"""Shared helper: enumerate routes that no authentication dependency guards.

Used by ``test_authz_route_sweep.py`` (the ratchet) and by
``tools/authz_sweep_report.py`` (the human-facing worklist).

Replaces the hand-maintained 18-path list in ``test_authz_regression.py``,
which was frozen at the 2026-06-24 sweep — nothing shipped afterwards was
covered, which is precisely how six unauthenticated ``/api/payments/*``
endpoints stayed open for months.

"Ungated" here means: walking the FastAPI dependency tree for the route, no
dependency resolves to a known authentication callable. That is a structural
check, not proof of a vulnerability — some routes are unauthenticated *by
design* (webhooks verify a signature, the public pay page authorizes with an
unguessable token, ``/health`` is meant to be open). Those live in the
baseline with a reason. The point of the ratchet is that adding a NEW one
becomes a deliberate, reviewed act.
"""
from __future__ import annotations

import inspect

# Callables that constitute authentication, by ``__qualname__``. A route is
# gated when any dependency in its tree resolves to one of these. Add to this
# set when a new auth dependency is introduced — otherwise its routes look
# ungated and the ratchet fires with a confusing message.
#
# Matched on QUALNAME, not name: several dependency factories return an inner
# function called ``_dependency``, and they are not equivalent —
# ``require_module`` only checks that a feature is enabled for the tenant and
# authenticates NOBODY, while ``require_permission`` / ``require_role`` reject
# an anonymous caller (their user lookup yields an empty dict, which satisfies
# no permission or role). Collapsing them by bare name marks every
# module-gated route as authenticated.
# ── the authentication / authorization seam ──────────────────────────────
#
# These two sets used to be one. Collapsing them meant the sweep could tell
# you a route had *someone* behind it, but never whether that someone was
# ALLOWED — `get_current_user` and `require_permission(...)` scored
# identically. That is why `routers/payments.py` could carry mutation routes
# with no permission gate and still show green.
#
# AUTHN answers "who are you"; AUTHZ answers "may you". A route needs both.
# `ungated_routes()` still unions them, so the authentication ratchet and
# `.authz_ungated_baseline` are unchanged by this split.
AUTHZ_DEPENDENCIES = frozenset(
    {
        # Permission / role gates proper.
        "require_permission.<locals>._dependency",
        "require_role.<locals>._dependency",
        "_require_admin",
        "_require_owner",
        "_require_dispatch",
        # Machine callers: the scope check, not the key check. `_require_api_key`
        # authenticates the caller; these decide what that caller may do.
        "scope_required",
        "_check_scope",
        # Verified to raise 403 for anything but admin/owner — that is an
        # authorization decision, not an identity one.
        "get_admin_principal",
        "get_admin_principal_for_ai_settings",
    }
)

AUTHN_DEPENDENCIES = frozenset(
    {
        # Staff / session
        "get_current_user",
        "get_current_active_user",
        "_current_user_dependency",
        "_get_current_user_safe",
        "get_user_for_views",
        "get_user_for_send",
        "get_user_for_oauth_start",
        # Customer portal
        "_current_portal_user",
        "_get_portal_principal",
        "get_current_portal_customer",
        # Admin / principal
        "get_current_principal",
        "get_current_principal_for_ai",
        # Machine callers
        "_require_api_key",
        # Signature- / secret-verified webhook callers
        "verify_inbound_email_secret",
    }
)

# Preserved under its original name: every caller of `ungated_routes()` and the
# `.authz_ungated_baseline` ratchet must see exactly the set they saw before.
AUTH_DEPENDENCIES = AUTHN_DEPENDENCIES | AUTHZ_DEPENDENCIES

# Explicitly NOT authentication, listed so the intent is on the record:
#   require_module.<locals>._dependency — feature flag, authenticates nobody
#   bind_tenant_context / get_db / get_tenant_db — plumbing
#   OAuth2PasswordBearer / HTTPBearer with auto_error=False — declares a
#     security scheme in the OpenAPI doc and then permits anonymous callers
#     through. This is exactly what made six /api/payments/* endpoints look
#     authenticated in review while enforcing nothing (2026-08-04).

_MAX_DEPTH = 8


# DELIBERATELY NOT counted as authentication: a bare security scheme
# (`OAuth2PasswordBearer` / `HTTPBearer`) even with ``auto_error=True``. It
# only asserts that an Authorization header is PRESENT and well-formed — it
# never validates the token, so `Authorization: Bearer garbage` satisfies it.
# Treating it as auth would reproduce, one flag away, the exact bug this sweep
# exists to catch (the /api/payments endpoints declared a scheme and read
# nothing). Authentication must be a callable that resolves and verifies a
# principal; those live in AUTH_DEPENDENCIES above.


def _dependency_names(dependant, depth: int = 0) -> set[str]:
    """Every dependency callable qualname in this route's tree.

    Recurses because auth is frequently nested — e.g. a router-level
    ``Depends(_require_owner)`` that itself depends on ``get_current_user``.
    A flat, one-level check reports such routes as unauthenticated.
    """
    names: set[str] = set()
    if dependant is None or depth > _MAX_DEPTH:
        return names
    for sub in getattr(dependant, "dependencies", []) or []:
        call = getattr(sub, "call", None)
        name = (
            getattr(call, "__qualname__", None)
            or getattr(call, "__name__", None)
            or type(call).__name__
        )
        names.add(name)
        names |= _dependency_names(sub, depth + 1)
    return names


MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Authorization does not only live in dependencies. Several routers call a
# role gate from inside the handler body — `_require_admin(user)` alone appears
# 34 times. A dependency-only sweep books every one of those as unprotected and
# tells a reviewer to "add require_permission" to a route that is already
# admin-only, which is a semantic change, not hardening.
#
# This is a TEXT heuristic over the handler's source, so it is deliberately
# generous: a marker inside a comment or a docstring counts, and a gate reached
# through a helper two calls deep does not. It exists to keep false ACCUSATIONS
# out of the baseline, and it errs toward silence.
IN_BODY_AUTHZ_MARKERS = (
    "_require_admin(",
    "_require_owner(",
    "_is_admin(",
    "_gate_browser(",
    "_OWNER_ROLES",
    "require_permission(",
    "require_role(",
)


def _enforces_authz_in_body(route) -> bool:
    """True when the handler's own source calls a role/permission gate."""
    fn = getattr(route, "endpoint", None)
    if fn is None:
        return False
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):  # C-level, or source unavailable
        return False
    return any(m in src for m in IN_BODY_AUTHZ_MARKERS)


_IN_BODY_SENTINEL = "<authz-enforced-in-handler-body>"


def _first_registration_dependencies(app=None) -> dict[str, set[str]]:
    """``{"METHOD /path": {dependency qualnames}}`` for reachable registrations.

    Shared by both sweeps so they can never disagree about which route table
    they are judging.
    """
    if app is None:
        from gdx_dispatch.app import create_app

        app = create_app()

    from gdx_dispatch.tests.conftest import iter_app_routes

    # FastAPI resolves first-match-wins, so when a (method, path) is registered
    # twice only the FIRST registration is reachable. Judge that one and ignore
    # the shadowed duplicate; reporting it would be a phantom finding.
    #
    # ⚠️ But "shadowed" is only as durable as the thing doing the shadowing.
    # This comment used to cite `GET /api/payments` as the example: ui_compat's
    # authenticated handler registers first, with an unauthenticated twin sat
    # unreachable behind it. `app.py` wraps that import in a try/except which
    # substitutes an EMPTY router on failure — and with that branch taken the
    # twin became the live route. Proven 2026-08-23 against a real container:
    # HTTP 200, 200 payment rows, no credentials. The twin is now deleted
    # (M17), so the example is gone.
    #
    # The lesson survives it: an ungated route excused as unreachable is a
    # hole waiting for an import to fail. Prefer deleting the twin to trusting
    # the shadow.
    out: dict[str, set[str]] = {}
    for path, route in iter_app_routes(app):
        dependant = getattr(route, "dependant", None)
        if dependant is None:  # mounts, static files, websockets
            continue
        names = _dependency_names(dependant)
        if _enforces_authz_in_body(route):
            names = names | {_IN_BODY_SENTINEL}
        for method in getattr(route, "methods", None) or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            out.setdefault(f"{method} {path}", names)
    return out


def ungated_routes(app=None) -> list[str]:
    """Sorted ``"METHOD /path"`` strings for every route without auth."""
    return sorted(
        key
        for key, names in _first_registration_dependencies(app).items()
        if not (names & AUTH_DEPENDENCIES)
    )


def unpermissioned_mutations(app=None) -> list[str]:
    """Mutation routes that authenticate a caller but never ask if they may.

    The gap ``ungated_routes()`` structurally cannot see: it unions
    authentication and authorization, so a route carrying only
    ``get_current_user`` scores exactly like one carrying
    ``require_permission("invoices.write")``.

    Restricted to POST/PUT/PATCH/DELETE deliberately. A read that only needs a
    logged-in user is an ordinary design choice; a *write* that never consults
    the 61-key permission catalog means the roles a tenant configures do not
    constrain that route at all.

    This is a structural check, not proof of a vulnerability — some mutations
    are legitimately open to any authenticated staff user, and the customer
    portal's own writes are authorized by an unguessable token rather than a
    permission key. Those live in the baseline with a reason.
    """
    return sorted(
        key
        for key, names in _first_registration_dependencies(app).items()
        if key.split(" ", 1)[0] in MUTATION_METHODS
        and (names & AUTHN_DEPENDENCIES)
        and not (names & AUTHZ_DEPENDENCIES)
        and _IN_BODY_SENTINEL not in names
    )
