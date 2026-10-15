"""``require_role`` / ``require_permission`` answer 401 when there is no principal.

A route's ``dependencies=[...]`` run before its parameter dependencies, so on a
route like ``GET /api/dispatch/scheduled-unassigned`` the permission gate runs
before ``get_current_user`` can 401 an expired token. The gate saw an empty
permission set and answered 403 "Missing permission", which the SPA shows as
"Permission denied" instead of sending the user back to log in.

No live token is "who are you?" (401); a live token without the grant is
"you may not" (403). These pin both halves with the exact status code, through
the same throwaway app ``test_bearer_fallback_typ_gate.py`` uses.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.modules import require_permission, require_role
from gdx_dispatch.routers.auth.core import _issue

# Non-UUID subject: the permission set comes from the role claim, no DB.
SUB = "gate-user"


def _app() -> FastAPI:
    app = FastAPI()
    app.state.denylist = Denylist()
    app.dependency_overrides[get_db] = lambda: None

    @app.get("/role", dependencies=[Depends(require_role("admin"))])
    def _role() -> dict:
        return {"ok": True}

    @app.get("/perm", dependencies=[Depends(require_permission("customers.read_all"))])
    def _perm() -> dict:
        return {"ok": True}

    # Builtin admin holds every key except billing.write.
    @app.get("/perm-billing", dependencies=[Depends(require_permission("billing.write"))])
    def _perm_billing() -> dict:
        return {"ok": True}

    return app


def _status(path: str, headers: dict | None = None):
    with TestClient(_app()) as tc:
        return tc.get(path, headers=headers or {})


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_expired_access_token_is_401(path):
    token, _ = _issue(SUB, "t", "admin", -60, "access")
    r = _status(path, {"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "Invalid or expired access token"
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_no_token_is_401(path):
    assert _status(path).status_code == 401


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_refresh_token_is_401(path):
    token, _ = _issue(SUB, "t", "admin", 600, "refresh")
    assert _status(path, {"Authorization": f"Bearer {token}"}).status_code == 401


def test_live_token_with_the_wrong_role_is_still_403():
    token, _ = _issue(SUB, "t", "technician", 600, "access")
    r = _status("/role", {"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Insufficient role"


def test_live_token_without_the_permission_is_still_403():
    token, _ = _issue(SUB, "t", "admin", 600, "access")
    r = _status("/perm-billing", {"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"].startswith("Missing permission")
