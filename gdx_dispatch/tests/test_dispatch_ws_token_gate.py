"""The dispatch board WebSocket handshake refuses a revoked access token.

``/ws/dispatch`` takes its JWT as a query parameter and decodes it itself, so
it never reaches ``get_current_user``. Before GDXA-1188 that decode checked
``typ`` but not the revocation denylist: a token revoked by logout or
``/auth/admin/revoke`` still opened the board socket until it expired (up to
15 minutes). Offboarding (``revoke_user_sessions``) does not put the access
token on the denylist, so this check does not stop it (GDXA-1215). It now goes through ``decode_live_access_token``, the
same check as the bearer fallback in core/modules.py.

The check used to run at the handshake only, so a socket already open when
its token was revoked or expired, or its user deleted or demoted, stayed open
(GDXA-1440). The receive loop now re-runs the whole gate below (token, then
user and permissions) on every frame, at least every ``_RECHECK_SECONDS`` and
at the token's exp, and closes with 1008; a broken check closes with 1011.

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
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
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
def board_db(monkeypatch, tmp_path):
    """The handler's user-row check and permission lookup read this DB.

    A file, not ``sqlite://`` on a StaticPool: an open socket's recheck reads
    it from the threadpool while the test thread inserts users and another
    socket handshakes, and one shared connection let a recheck's checkin
    rollback swallow a concurrent insert (GDXA-1440)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'board.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    TenantBase.metadata.create_all(engine, checkfirst=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(dispatch_ws, "SessionLocal", factory)
    monkeypatch.setattr(database, "app_engine", engine)
    with factory() as db:
        yield db
    engine.dispose()


def _user(db, role: str, **fields) -> str:
    uid = uuid4()
    db.add(User(id=uid, email=f"{uid.hex[:8]}@example.com", username=uid.hex[:8], role=role,
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
    """A failing revocation check does not admit the token, and the handler
    closes with 1011 (internal error), not the 1008 of a refusal.

    That code difference shows only under TestClient. The close comes before
    accept, and uvicorn answers every pre-accept close with HTTP 403, so a
    real client sees the same 403 for a broken denylist as for a revoked
    token. In production only the server log tells them apart:
    ``decode_live_access_token``'s denylist traceback, then the handler's
    ``dispatch_ws_handshake_closed_1011`` warning."""
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


# ── GDXA-1440: an open socket re-checks its token ────────────────────────


def _closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    return exc.value.code


def test_token_revoked_while_open_closes_the_idle_socket(app, dispatcher, monkeypatch):
    """A socket that sends nothing is still closed once its token is revoked:
    the loop wakes on its own and re-checks."""
    monkeypatch.setattr(dispatch_ws, "_RECHECK_SECONDS", 0.1)
    token, jti = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_json({"type": "board_refresh"})
        assert ws.receive_json() == {"type": "board_refresh"}
        app.state.denylist.add(jti, datetime.now(UTC) + timedelta(minutes=10))
        assert _closed_with(ws) == 1008
    assert not manager._connections.get(TENANT)


def test_revoked_token_frame_is_not_rebroadcast(app, board_db, dispatcher):
    """The frame that arrives after revocation is checked before it is
    rebroadcast: the sender is closed and the watcher never sees it."""
    token, jti = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    watcher_token, _ = _issue(_user(board_db, "sales"), TENANT, "sales", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={watcher_token}") as watcher:
        with tc.websocket_connect(f"/ws?token={token}") as board:
            board.send_json({"type": "board_refresh"})
            assert board.receive_json() == {"type": "board_refresh"}
            assert watcher.receive_json() == {"type": "board_refresh"}
            app.state.denylist.add(jti, datetime.now(UTC) + timedelta(minutes=10))
            board.send_json({"type": "job_assigned", "job_id": "j-1", "technician_id": "t-1"})
            assert _closed_with(board) == 1008
        # Still connected, the watcher's next frame is a fresh dispatcher's,
        # not the revoked one's job_assigned.
        fresh, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
        with tc.websocket_connect(f"/ws?token={fresh}") as board2:
            board2.send_json({"type": "board_refresh"})
            assert board2.receive_json() == {"type": "board_refresh"}
        assert watcher.receive_json() == {"type": "board_refresh"}


def test_token_expiring_while_open_closes_at_exp(app, dispatcher):
    """No recheck patch: the loop wakes at the token's exp, not after the
    30-second interval."""
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 1, "access")
    started = time.monotonic()
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={token}") as ws:
        assert _closed_with(ws) == 1008
    assert time.monotonic() - started < 5


def test_denylist_failure_while_open_closes_1011(app, dispatcher, monkeypatch):
    monkeypatch.setattr(dispatch_ws, "_RECHECK_SECONDS", 0.1)
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_json({"type": "board_refresh"})
        assert ws.receive_json() == {"type": "board_refresh"}
        app.state.denylist = _BrokenDenylist()
        assert _closed_with(ws) == 1011


def test_live_token_survives_the_recheck(app, dispatcher, monkeypatch):
    """The wake does not close a socket whose token is still live."""
    monkeypatch.setattr(dispatch_ws, "_RECHECK_SECONDS", 0.05)
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={token}") as ws:
        time.sleep(0.3)
        ws.send_json({"type": "board_refresh"})
        assert ws.receive_json() == {"type": "board_refresh"}


def _set_user(db, uid: str, **fields) -> None:
    db.expire_all()
    user = db.get(User, UUID(uid))
    for k, v in fields.items():
        setattr(user, k, v)
    db.commit()


@pytest.mark.parametrize("fields", [
    {"deleted_at": datetime.now(UTC)},
    {"active": False},
    {"role": "technician"},
], ids=["deleted", "deactivated", "demoted_off_the_board"])
def test_user_change_while_open_closes_and_drops_the_frame(app, board_db, dispatcher, fields):
    """These revoke only refresh tokens, never the access token's jti, so the
    token check alone kept the socket broadcasting until exp."""
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    watcher_token, _ = _issue(_user(board_db, "sales"), TENANT, "sales", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={watcher_token}") as watcher:
        with tc.websocket_connect(f"/ws?token={token}") as board:
            board.send_json({"type": "board_refresh"})
            assert board.receive_json() == {"type": "board_refresh"}
            assert watcher.receive_json() == {"type": "board_refresh"}
            _set_user(board_db, dispatcher, **fields)
            board.send_json({"type": "job_assigned", "job_id": "j-1", "technician_id": "t-1"})
            assert _closed_with(board) == 1008
        fresh, _ = _issue(_user(board_db, "dispatcher"), TENANT, "dispatcher", 600, "access")
        with tc.websocket_connect(f"/ws?token={fresh}") as board2:
            board2.send_json({"type": "board_refresh"})
            assert board2.receive_json() == {"type": "board_refresh"}
        assert watcher.receive_json() == {"type": "board_refresh"}


def test_demotion_to_read_only_keeps_watching_but_stops_broadcast(app, board_db, dispatcher):
    """A dispatcher demoted to sales still holds jobs.read_all: the socket
    stays, but may_send is recomputed and its frames are dropped."""
    token, _ = _issue(dispatcher, TENANT, "dispatcher", 600, "access")
    with TestClient(app) as tc, tc.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_json({"type": "board_refresh"})
        assert ws.receive_json() == {"type": "board_refresh"}
        _set_user(board_db, dispatcher, role="sales")
        ws.send_json({"type": "job_assigned", "job_id": "j-1", "technician_id": "t-1"})
        other, _ = _issue(_user(board_db, "dispatcher"), TENANT, "dispatcher", 600, "access")
        with tc.websocket_connect(f"/ws?token={other}") as board:
            board.send_json({"type": "board_refresh"})
            assert board.receive_json() == {"type": "board_refresh"}
        # The demoted socket's first frame back is the other dispatcher's.
        assert ws.receive_json() == {"type": "board_refresh"}
