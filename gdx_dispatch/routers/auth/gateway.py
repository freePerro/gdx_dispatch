"""BFF auth gateway router (SS-12 Slice A — scaffold only; SS-13 Slice B
adds ``return_to=auto`` picker handling on ``GET /auth/login``).

This router establishes the backbone for the Backend-For-Frontend (BFF)
auth gateway that will mediate between the Vue SPA and Authentik. All
endpoints in this slice are deterministic scaffold placeholders — real
OAuth/PKCE/token-exchange mechanics are deferred to SS-12B+.

Deferred to SS-12B+ (DO NOT implement here):
- PKCE challenge/verifier generation
- Authentik authorization URL construction
- Authorization-code token exchange
- Refresh token rotation
- Cookie set/clear side effects
- Tenant resolution or switch-tenant logic

Scaffold endpoints:
- GET  /auth/login           placeholder for login redirect initiation
                             (SS-13 Slice B: also dispatches the
                             ``return_to=auto`` login-picker redirect)
- GET  /auth/callback        placeholder for OAuth callback handler
- POST /auth/refresh         placeholder for refresh flow
- POST /auth/logout          placeholder for logout flow
- POST /auth/switch-tenant   placeholder for tenant-switch flow

The router shares the ``/auth`` prefix with ``gdx_dispatch.routers.auth`` because
SS-12 will eventually replace the legacy endpoints with the BFF flow.
During SS-12A the legacy router keeps precedence for the POST-collision
paths (``/auth/refresh``, ``/auth/logout``) because it is registered
first in ``gdx_dispatch/app.py``; scaffold happy-path tests therefore exercise
this router against an isolated ``FastAPI`` test app so the scaffold
marker is observable without touching legacy behavior.

Test-performance note (SS-12A redo r3): the scaffold carries no lifespan,
no middleware, and no external I/O. ``gdx_dispatch/tests/test_auth_gateway_scaffold.py``
verifies route contribution against ``router.routes`` directly (r2) and
verifies the main-app wiring via a static source check on ``gdx_dispatch/app.py``
— neither path invokes ``create_app()``, because that import-heavy
construction blew past Codex's 90s replay budget in earlier revisions.
r3 additionally enters the isolated-app ``TestClient`` as a context
manager in the ``gateway_only_client`` fixture so one anyio
``BlockingPortal`` is shared by every scaffold call and is shut down
deterministically at module teardown; r2's bare ``TestClient`` return
spawned a fresh portal per call and stalled Codex's container dispatch
at the first request-issuing test. If SS-12B+ adds I/O, env
reads, or cookie side effects, those tests will need to grow runtime
coverage — and the precedence contract below will need to be revisited
if the legacy ``auth`` include order changes.

SS-13 Slice B — login-picker auto-redirect
------------------------------------------
When the marketing Sign In link hits ``GET /auth/login?return_to=auto``,
this handler calls the SS-13 Slice A logic (``gdx_dispatch.routers.me.list_my_tenants``)
and issues a 302 redirect keyed off the membership count:

- 0 memberships → ``/signup``
- 1 membership  → ``/t/<slug>/`` (single-tenant fast path)
- 2+ memberships → ``/login-picker`` (Slice B SPA view lands next)

Any other ``return_to`` value (or the query parameter absent) preserves
the SS-12A scaffold payload so the direct-coroutine scaffold tests and
the stable ``SCAFFOLD_MARKER`` contract remain intact. The auth +
session dependencies are declared with ``Depends()`` defaults so the
``asyncio.run(login_scaffold())`` test path keeps working — the scaffold
branch never touches ``user`` or ``db``.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.me import list_my_tenants

log = logging.getLogger(__name__)

SCAFFOLD_MARKER = "ss12a_scaffold_placeholder"

router = APIRouter(prefix="/auth", tags=["auth_gateway"])


def _scaffold_payload(endpoint: str) -> dict[str, Any]:
    return {
        "status": SCAFFOLD_MARKER,
        "endpoint": endpoint,
        "slice": "ss12-a",
        "note": "scaffold-only; real BFF mechanics land in SS-12B+",
    }


@router.get("/login", name="auth_gateway_login_scaffold")
async def login_scaffold(
    return_to: str | None = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """Scaffold for BFF login initiation. SS-12B will add PKCE + redirect.

    SS-13 Slice B: when ``return_to=auto`` is supplied, dispatch to the
    login-picker redirect based on the authenticated user's membership
    count. All other invocations preserve the SS-12A scaffold payload so
    the existing scaffold tests (direct-coroutine ``asyncio.run``) still
    pass — ``user`` / ``db`` are only read inside the auto branch.
    """
    if return_to == "auto":
        tenants = list_my_tenants(user=user, db=db)
        if not tenants:
            return RedirectResponse(url="/signup", status_code=302)
        if len(tenants) == 1:
            slug = tenants[0]["slug"]
            return RedirectResponse(url=f"/t/{slug}/", status_code=302)
        return RedirectResponse(url="/login-picker", status_code=302)
    return _scaffold_payload("login")


@router.get("/callback", name="auth_gateway_callback_scaffold")
async def callback_scaffold() -> dict[str, Any]:
    """Scaffold for OAuth callback. SS-12B will add token exchange + cookies."""
    return _scaffold_payload("callback")


@router.post("/refresh", name="auth_gateway_refresh_scaffold")
async def refresh_scaffold() -> dict[str, Any]:
    """Scaffold for refresh flow. SS-12B will add cookie-bound rotation."""
    return _scaffold_payload("refresh")


@router.post("/logout", name="auth_gateway_logout_scaffold")
async def logout_scaffold() -> dict[str, Any]:
    """Scaffold for logout flow. SS-12B will add cookie clear + session revoke."""
    return _scaffold_payload("logout")


@router.post("/switch-tenant", name="auth_gateway_switch_tenant_scaffold")
async def switch_tenant_scaffold() -> dict[str, Any]:
    """Scaffold for tenant switch. SS-12B+ will add tenant resolution."""
    return _scaffold_payload("switch-tenant")
