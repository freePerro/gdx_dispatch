"""Logout puts the access token it was handed on the denylist (GDXA-1215).

Before the fix ``POST /auth/logout`` revoked the refresh token and deleted
the cookies, nothing more: a copied access token kept working for the rest
of its 15-minute TTL on every path that trusts the token, among them the
bearer fallback behind ``require_role`` and the ``/ws/dispatch`` handshake.

Every test here does a real logout over HTTP through the real auth router,
then presents the same token on a real route or a real WebSocket handshake
in the same app, so the denylist the logout wrote is the one the reader
checks. Only the refresh-token Redis set and the audit writer are stubbed.
"""
from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.core.websocket import manager
from gdx_dispatch.routers.auth import core as auth_core
from gdx_dispatch.routers.auth.core import _issue, get_current_user
from gdx_dispatch.routers.dispatch_ws import dispatch_board_ws


class _FakeRefreshRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}

    def sadd(self, key: str, value: str) -> None:
        self.sets.setdefault(key, set()).add(value)

    def expire(self, key: str, ttl: int) -> None:
        pass


class _StubDb:
    def commit(self) -> None:
        pass


@pytest.fixture
def audit_rows(monkeypatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        auth_core, "log_audit_event_sync", lambda _db, **kw: rows.append(kw),
    )
    return rows


@pytest.fixture
def client(monkeypatch, audit_rows) -> TestClient:
    monkeypatch.setattr(auth_core, "redis", _FakeRefreshRedis())
    app = FastAPI()
    app.state.denylist = Denylist()
    app.include_router(auth_core.router)
    app.dependency_overrides[get_db] = lambda: _StubDb()

    # Gated only by require_role: no get_current_user, so the bearer
    # fallback in core/modules.py is what admits or refuses the token.
    @app.get("/role-gated", dependencies=[Depends(require_role("dispatcher"))])
    def role_gated() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/user-gated")
    def user_gated(user: Annotated[dict, Depends(get_current_user)]) -> dict[str, str]:
        return {"user_id": user["user_id"]}

    app.add_api_websocket_route("/ws", dispatch_board_ws)
    manager._connections.clear()
    with TestClient(app) as tc:
        yield tc


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ws_close_code(client: TestClient, token: str) -> int | None:
    """None when the socket was accepted, else the code it was closed with."""
    try:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_json({"type": "board_refresh"})
            assert ws.receive_json() == {"type": "board_refresh"}
    except WebSocketDisconnect as exc:
        return exc.code
    return None


def test_bearer_logout_then_the_same_token_is_refused_everywhere(client, audit_rows):
    token, jti = _issue("dispatcher-1", "t", "dispatcher", 600, "access")
    # Falsifier: the token works on all three before logout, so a refusal
    # afterwards is the logout's doing, not a broken fixture.
    assert client.get("/role-gated", headers=_bearer(token)).status_code == 200
    assert client.get("/user-gated", headers=_bearer(token)).status_code == 200
    assert _ws_close_code(client, token) is None

    assert client.post("/auth/logout", headers=_bearer(token)).status_code == 200

    assert client.get("/role-gated", headers=_bearer(token)).status_code == 403
    assert client.get("/user-gated", headers=_bearer(token)).status_code == 401
    assert _ws_close_code(client, token) == 1008

    logout = [r for r in audit_rows if r["action"] == "logout"]
    assert len(logout) == 1
    assert logout[0]["details"] == {
        "access_tokens_revoked": [{"sub": "dispatcher-1", "jti": jti}],
    }
    assert logout[0]["user_id"] == "dispatcher-1"


def test_cookie_logout_revokes_the_cookie_token_and_the_refresh_token(client):
    access, _ = _issue("dispatcher-2", "t", "dispatcher", 600, "access")
    refresh, refresh_jti = _issue("dispatcher-2", "t", "dispatcher", 3600, "refresh")
    client.cookies.set("access_token", access)
    client.cookies.set("refresh_token", refresh)

    assert client.post("/auth/logout").status_code == 200
    client.cookies.clear()

    # The copied cookie value, replayed as a bearer, is now refused.
    assert client.get("/role-gated", headers=_bearer(access)).status_code == 403
    assert _ws_close_code(client, access) == 1008
    assert refresh_jti in auth_core.redis.sets["used_refresh_jtis"]


def test_logout_revokes_only_the_tokens_it_was_handed(client):
    # Another session of the same user (another device) is not ended by
    # this logout: the denylist is per token, not per user.
    this_device, _ = _issue("dispatcher-3", "t", "dispatcher", 600, "access")
    other_device, _ = _issue("dispatcher-3", "t", "dispatcher", 600, "access")

    assert client.post("/auth/logout", headers=_bearer(this_device)).status_code == 200

    assert client.get("/role-gated", headers=_bearer(this_device)).status_code == 403
    assert client.get("/role-gated", headers=_bearer(other_device)).status_code == 200


def test_a_forged_token_at_logout_revokes_nothing(client, audit_rows):
    # Someone who knows a victim's jti but not the signing key must not be
    # able to log the victim out by presenting a forged token naming it.
    import jwt as pyjwt

    victim, victim_jti = _issue("dispatcher-4", "t", "dispatcher", 600, "access")
    claims = pyjwt.decode(victim, options={"verify_signature": False})
    forged = pyjwt.encode(claims, "not-the-signing-key-" * 3, algorithm="HS256")

    assert client.post("/auth/logout", headers=_bearer(forged)).status_code == 200

    assert client.get("/role-gated", headers=_bearer(victim)).status_code == 200
    assert client.app.state.denylist.contains(victim_jti) is False
    assert audit_rows[-1]["details"] == {"access_tokens_revoked": []}


def test_logout_with_no_token_still_succeeds(client, audit_rows):
    assert client.post("/auth/logout").status_code == 200
    assert audit_rows[-1]["user_id"] == "anonymous"
    assert audit_rows[-1]["details"] == {"access_tokens_revoked": []}


def test_logout_succeeds_when_the_denylist_write_fails(client, monkeypatch):
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("denylist down")

    monkeypatch.setattr(client.app.state.denylist, "add", boom)
    token, _ = _issue("dispatcher-5", "t", "dispatcher", 600, "access")
    resp = client.post("/auth/logout", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_logout_with_redis_down_still_clears_cookies_and_audits(client, audit_rows):
    class _DownRedis:
        def sadd(self, *_a: Any) -> None:
            raise ConnectionError("redis down")

        def expire(self, *_a: Any) -> None:
            raise ConnectionError("redis down")

    auth_core.redis = _DownRedis()  # restored by the fixture's monkeypatch
    access, _ = _issue("dispatcher-6", "t", "dispatcher", 600, "access")
    refresh, _ = _issue("dispatcher-6", "t", "dispatcher", 3600, "refresh")
    client.cookies.set("refresh_token", refresh)

    resp = client.post("/auth/logout", headers=_bearer(access))

    assert resp.status_code == 200
    assert "refresh_token=" in resp.headers.get("set-cookie", "")
    assert audit_rows[-1]["action"] == "logout"
    assert client.get("/role-gated", headers=_bearer(access)).status_code == 403


def test_audit_names_the_owner_of_each_revoked_token(client, audit_rows):
    # The refresh cookie names who is logging out; a bearer belonging to
    # someone else is still recorded under its own sub.
    other, other_jti = _issue("someone-else", "t", "dispatcher", 600, "access")
    refresh, _ = _issue("me", "t", "dispatcher", 3600, "refresh")
    client.cookies.set("refresh_token", refresh)

    assert client.post("/auth/logout", headers=_bearer(other)).status_code == 200

    assert audit_rows[-1]["user_id"] == "me"
    assert audit_rows[-1]["details"] == {
        "access_tokens_revoked": [{"sub": "someone-else", "jti": other_jti}],
    }
