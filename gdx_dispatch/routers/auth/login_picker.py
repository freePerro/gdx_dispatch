"""SS-13 Slice D — explicit backend route for the login picker page.

Serves ``GET /login-picker`` by returning the Vue SPA ``index.html`` so
the route is explicitly registered in the FastAPI route table (visible
to OpenAPI tooling, route-inventory scripts, and integration tests)
rather than silently falling through the catch-all at
``gdx_dispatch/app.py:serve_spa``.

The actual picker UI is rendered client-side by
``gdx_dispatch/frontend/src/views/LoginPicker.vue`` via Vue Router; the backend
just has to hand back the SPA entry point. Authentication is NOT
enforced here — the picker page itself is public, and the
``GET /api/me/tenants`` call the SPA makes on mount (SS-13 Slice A)
is what requires the bearer token.

Slice flow:
* marketing Sign In (landing page) → ``app.example.com/auth/login?return_to=auto``
* Slice B gateway (``gdx_dispatch.routers.auth.gateway``) → 302 ``/login-picker``
  when the caller has 2+ memberships
* this router → returns ``index.html`` → Vue Router mounts ``LoginPicker.vue``
* ``LoginPicker.vue`` calls ``GET /api/me/tenants`` (Slice A) and renders
  the button list
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth_gateway"])

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@router.get("/login-picker", include_in_schema=False, name="login_picker_page")
def login_picker_page():
    """Return the Vue SPA entry point for the ``/login-picker`` route.

    Falls back to an explicit 503 + logged warning when the frontend
    has not been built — matches the behavior of the SPA catch-all at
    ``gdx_dispatch/app.py`` so deploy-time drift (missing ``dist/index.html``) is
    surfaced instead of silently serving a blank page.
    """
    index = _FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(str(index))
    log.warning(
        "login_picker_page: frontend index.html not found at %s — returning 503",
        index,
    )
    return HTMLResponse("<h1>Frontend not built</h1>", status_code=503)
