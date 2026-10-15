"""The bearer-token fallback in core/modules.py accepts only live access tokens.

``require_role`` and ``require_permission`` (via ``_resolve_request_user``)
fall back to decoding the Authorization header themselves when no principal is
on the request. Before GDXA-1147 that decode skipped the ``typ`` check
``get_current_user`` applies, so a 30-day refresh token, an MCP token or a
browser-proxy ticket, all signed with the same key, passed as a user on any
route gated only by these dependencies. ``_resolve_request_user`` also skipped
the revocation denylist.

These are real requests through a throwaway FastAPI app with no
``get_current_user`` override and no ``request.state`` principal, so the
fallback is the only path that can admit them.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.modules import require_permission, require_role
from gdx_dispatch.routers.auth.core import _issue

# A non-UUID subject keeps _load_user_permissions off the database: the
# permission set comes from the token's role claim (admin -> BUILTIN admin).
SUB = "fallback-user"
PERM = "customers.read_all"


def _app() -> FastAPI:
    app = FastAPI()
    app.state.denylist = Denylist()
    app.dependency_overrides[get_db] = lambda: None

    @app.get("/role", dependencies=[Depends(require_role("admin"))])
    def _role() -> dict:
        return {"ok": True}

    @app.get("/perm", dependencies=[Depends(require_permission(PERM))])
    def _perm() -> dict:
        return {"ok": True}

    return app


@pytest.fixture
def app() -> FastAPI:
    return _app()


def _get(app: FastAPI, path: str, token: str) -> int:
    with TestClient(app) as tc:
        return tc.get(path, headers={"Authorization": f"Bearer {token}"}).status_code


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_access_token_still_passes(app, path):
    token, _ = _issue(SUB, "t", "admin", 600, "access")
    assert _get(app, path, token) == 200


@pytest.mark.parametrize("path", ["/role", "/perm"])
@pytest.mark.parametrize("typ", ["refresh", "mcp_access", "browser"])
def test_non_access_typ_is_refused(app, path, typ):
    token, _ = _issue(SUB, "t", "admin", 600, typ)
    assert _get(app, path, token) in (401, 403)


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_real_mcp_token_with_admin_role_is_refused(app, path):
    """A token from the real MCP minter is signed with the login key (same
    JWT_SECRET) and, given a role claim, must still be refused. This passed
    before GDXA-1147 too: the fallback's decode passes no ``audience``, so
    PyJWT rejects the MCP token's ``aud`` claim. It pins that refusal; the
    ``mcp_access`` case above pins the ``typ`` check itself."""
    from gdx_dispatch.core.mcp_bearer import mint_mcp_access_token

    token = mint_mcp_access_token(
        tenant_id="t", subject_id=SUB, issuer="https://h", audience="https://h/mcp",
        extra={"role": "admin"},
    )
    assert _get(app, path, token) in (401, 403)


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_revoked_access_token_is_refused(app, path):
    token, jti = _issue(SUB, "t", "admin", 600, "access")
    assert _get(app, path, token) == 200
    app.state.denylist.add(jti, datetime.now(UTC) + timedelta(minutes=10))
    assert _get(app, path, token) in (401, 403)


@pytest.mark.parametrize("path", ["/role", "/perm"])
@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c"])
def test_undecodable_token_is_refused(app, path, token):
    assert _get(app, path, token) in (401, 403)


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_expired_access_token_is_refused(app, path):
    token, _ = _issue(SUB, "t", "admin", -60, "access")
    assert _get(app, path, token) in (401, 403)


class _BrokenDenylist:
    def contains(self, jti: str) -> bool:
        raise RuntimeError("denylist backend broke")


@pytest.mark.parametrize("path", ["/role", "/perm"])
def test_denylist_failure_is_503_not_admitted(app, path):
    """An error escaping the revocation check neither admits the token nor
    passes for "Insufficient role": it is a 503 the operator can see."""
    app.state.denylist = _BrokenDenylist()
    token, _ = _issue(SUB, "t", "admin", 600, "access")
    assert _get(app, path, token) == 503
