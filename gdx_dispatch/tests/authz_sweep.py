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
AUTH_DEPENDENCIES = frozenset(
    {
        # Staff / session
        "get_current_user",
        "get_current_active_user",
        "_current_user_dependency",
        "_get_current_user_safe",
        "get_user_for_views",
        "get_user_for_send",
        "get_user_for_oauth_start",
        "require_permission.<locals>._dependency",
        "require_role.<locals>._dependency",
        "_require_admin",
        "_require_owner",
        "_require_dispatch",
        # Customer portal
        "_current_portal_user",
        "_get_portal_principal",
        "get_current_portal_customer",
        # Admin / principal
        "get_admin_principal",
        "get_admin_principal_for_ai_settings",
        "get_current_principal",
        "get_current_principal_for_ai",
        # Machine callers
        "_require_api_key",
        "scope_required",
        "_check_scope",
        # Signature-verified webhook callers
        "verify_twilio_signature",
    }
)

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


def ungated_routes(app=None) -> list[str]:
    """Sorted ``"METHOD /path"`` strings for every route without auth."""
    if app is None:
        from gdx_dispatch.app import create_app

        app = create_app()

    from gdx_dispatch.tests.conftest import iter_app_routes

    # FastAPI resolves first-match-wins, so when a (method, path) is registered
    # twice only the FIRST registration is reachable. Judge that one and ignore
    # the shadowed duplicate — e.g. `GET /api/payments` is served by ui_compat's
    # authenticated handler, while an unauthenticated twin sits unreachable
    # behind it. Reporting the shadowed one would be a phantom finding.
    verdict: dict[str, bool] = {}
    for path, route in iter_app_routes(app):
        dependant = getattr(route, "dependant", None)
        if dependant is None:  # mounts, static files, websockets
            continue
        gated = bool(_dependency_names(dependant) & AUTH_DEPENDENCIES)
        for method in getattr(route, "methods", None) or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            verdict.setdefault(f"{method} {path}", gated)
    return sorted(key for key, gated in verdict.items() if not gated)
