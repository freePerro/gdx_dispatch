"""The dispatch board WebSocket handshake refuses a revoked access token.

``/ws/dispatch`` takes its JWT as a query parameter and decodes it itself, so
it never reaches ``get_current_user``. Before GDXA-1188 that decode checked
``typ`` but not the revocation denylist: a token revoked by logout,
offboarding or admin revoke still opened the board socket until it expired
(up to 15 minutes). It now goes through ``decode_live_access_token``, the
same check as the bearer fallback in core/modules.py.

The check runs at the handshake only. A socket already open when its token
is revoked stays open; nothing here re-checks it.

A live token is not enough on its own (GDXA-1210): the handshake used to
admit any token carrying a tenant_id, whatever its role, and rebroadcast
whatever it sent. It now runs ``finalize_login_jwt``, the gate stack behind
``get_current_user`` (no users row, deleted, inactive, wrong tenant or
must-change-password are refused; users.role overrides the claim), resolves
the permissions the way ``require_permission`` does, and admits only
``jobs.read_all``, the key every /api/dispatch/* read is gated on. Frames are
rebroadcast only for ``jobs.write``, the key job assignment is gated on.

These are real WebSocket handshakes through a throwaway FastAPI app, so the
handler sees a real ``WebSocket`` whose ``app.state`` holds the denylist.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from gdx_dispatch.core import database
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.tenant import single_tenant
from gdx_dispatch.core.websocket import manager
from gdx_dispatch.models.tenant_models import TenantRole, User, UserRoleAssignment
from gdx_dispatch.routers import dispatch_ws
from gdx_dispatch.routers.auth.core import _issue
from gdx_dispatch.routers.dispatch_ws import dispatch_board_ws

TENANT = single_tenant()["id"]


@pytest.fixture(autouse=True)
def board_db(monkeypatch):
    """The handler's user-row check and permission lookup read this DB."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(dispatch_ws, "SessionLocal", factory)
    monkeypatch.setattr(database, "app_engine", engine)
    with factory() as db:
        yield db
    engine.dispose()


def _user(db, role: str, **fields) -> str:
    uid = uuid4()
    db.add(User(id=uid, email=f"{uid.hex[:8]}@e.co", username=uid.hex[:8], role=role,
                company_id=TENANT, **fields))
    db.commit()
    return str(uid)


@pytest.fixture
def dispatcher(board_db) -> str:
    return _user(board_db, "dispatcher")


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.state.denylist = Denylist()
    app.add_api_websocket_route("/ws", dispatch_board_ws)
    manager._connections.clear()
    return app


def _close_code(app: FastAPI, token: str) -> int | None:
    """None when the socket was accepted, else the code it was closed with."""
    with TestClient(app) as tc:
        try:
            with tc.websocket_connect(f"/ws?token={token}") as ws:
                ws.send_json({"type": "board_refresh"})
                assert ws.receive_json() == {"type": "board_refresh"}
        except WebSocketDisconnect as exc:
            return exc.code
    return None


def test_live_access_token_opens_the_socket(app, dispatcher):
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    assert _close_code(app, token) is None


def test_revoked_access_token_is_refused(app, dispatcher):
    token, jti = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    assert _close_code(app, token) is None
    app.state.denylist.add(jti, datetime.now(UTC) + timedelta(minutes=10))
    assert _close_code(app, token) == 1008


@pytest.mark.parametrize("typ", ["refresh", "mcp_access", "browser"])
def test_non_access_typ_is_refused(app, dispatcher, typ):
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, typ)
    assert _close_code(app, token) == 1008


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_missing_or_undecodable_token_is_refused(app, token):
    assert _close_code(app, token) == 1008


def test_expired_access_token_is_refused(app, dispatcher):
    token, _ = _issue(dispatcher, TENANT, "dispatcher", -60, "access")
    assert _close_code(app, token) == 1008


class _BrokenDenylist:
    def contains(self, jti: str) -> bool:
        raise RuntimeError("denylist backend broke")


def test_denylist_failure_closes_1011_not_admitted(app, dispatcher):
    """A failing revocation check neither admits the token nor reads as a
    policy refusal: the socket closes with 1011 (internal error)."""
    app.state.denylist = _BrokenDenylist()
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    assert _close_code(app, token) == 1011


# ── GDXA-1210: the role gate ─────────────────────────────────────────────


@pytest.mark.parametrize("role", ["customer", "technician", "accounting", ""])
def test_tenant_bearing_token_without_board_read_is_refused(app, board_db, role):
    """tenant_id alone used to open the board. A customer, a technician
    (jobs.read_own only) or an office role without jobs.read_all is refused."""
    token, _ = _issue(_user(board_db, role), TENANT, role, 600, "access")
    assert _close_code(app, token) == 1008


@pytest.mark.parametrize("role", ["owner", "admin", "dispatcher"])
def test_board_roles_are_admitted(app, board_db, role):
    token, _ = _issue(_user(board_db, role), TENANT, role, 600, "access")
    assert _close_code(app, token) is None


def test_users_role_in_the_db_outranks_the_token_claim(app, board_db):
    """A token minted as dispatcher for a user since demoted to technician is
    refused: users.role wins, as on REST."""
    token, _ = _issue(_user(board_db, "technician"), TENANT, "dispatcher", 600, "access")
    assert _close_code(app, token) == 1008


@pytest.mark.parametrize("fields", [
    {"deleted_at": datetime.now(UTC)},
    {"active": False},
    {"must_change_password": True},
], ids=["deleted", "deactivated", "must_change_password"])
def test_ineligible_user_with_a_live_token_is_refused(app, board_db, fields):
    """get_current_user refuses these on REST; the socket did not, because
    the role resolver falls back to the token's claim when no row matches."""
    token, _ = _issue(_user(board_db, "dispatcher", **fields), TENANT, "admin", 600, "access")
    assert _close_code(app, token) == 1008


@pytest.mark.parametrize("sub", ["dispatcher-1", str(uuid4())], ids=["not-a-uuid", "no-row"])
def test_token_with_no_users_row_is_refused(app, sub):
    token, _ = _issue(sub, TENANT, "dispatcher", 600, "access")
    assert _close_code(app, token) == 1008


def test_token_for_another_tenant_is_refused(app, dispatcher):
    token, _ = _issue(dispatcher, str(uuid4()), "dispatcher", 600, "access")
    assert _close_code(app, token) == 1008


def test_custom_role_snapshot_with_board_read_is_admitted(app, board_db):
    """A tenant-defined role is judged by its permissions, not its name."""
    uid = _user(board_db, "office")
    role = TenantRole(id=uuid4(), company_id=TENANT, name="office",
                      permissions=json.dumps(["jobs.read_all", "jobs.write"]), is_system=False)
    board_db.add(role)
    board_db.flush()
    board_db.add(UserRoleAssignment(id=uuid4(), company_id=TENANT, user_id=uid, role_id=role.id))
    board_db.commit()
    token, _ = _issue(uid, TENANT, "office", 600, "access")
    assert _close_code(app, token) is None


def test_read_only_holder_watches_but_cannot_broadcast(app, board_db, dispatcher):
    """sales holds jobs.read_all but not jobs.write: it sees the board's
    frames, and what it sends is dropped rather than rebroadcast."""
    sales, _ = _issue(_user(board_db, "sales"), TENANT, "sales", 600, "access")
    board_token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={sales}") as watcher:
        watcher.send_json({"type": "job_assigned", "job_id": "j-1", "technician_id": "t-1"})
        with tc.websocket_connect(f"/ws?token={board_token}") as board:
            board.send_json({"type": "board_refresh"})
            assert board.receive_json() == {"type": "board_refresh"}
        # The first frame the watcher sees is the dispatcher's, not its own.
        assert watcher.receive_json() == {"type": "board_refresh"}
