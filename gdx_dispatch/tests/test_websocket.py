from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import os

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from gdx_dispatch.core.websocket import ConnectionManager, manager
from gdx_dispatch.routers.dispatch_ws import dispatch_board_ws

# Match the JWT_SECRET set in gdx_dispatch/tests/conftest.py so signatures verify.
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
ALG = "HS256"


class DummyWebSocket:
    def __init__(self, incoming: list[dict] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.accepted = False
        self.closed_code: int | None = None
        self.sent: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.closed_code = code

    async def receive_json(self) -> dict:
        if not self.incoming:
            raise WebSocketDisconnect(code=1000)
        return self.incoming.pop(0)

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def make_token(tenant_id: str, sub: str = "dispatcher-1") -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    claims = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": "dispatcher",
        "typ": "access",
        "exp": exp,
    }
    return jwt.encode(claims, JWT_SECRET, algorithm=ALG)


@pytest.fixture(autouse=True)
def clear_manager_registry() -> None:
    manager._connections.clear()


def test_connect() -> None:
    async def _run() -> None:
        ws = DummyWebSocket()
        await dispatch_board_ws(ws, token=make_token("tenant-a"))
        assert ws.accepted is True

    asyncio.run(_run())


def test_tenant_isolation() -> None:
    async def _run() -> None:
        cm = ConnectionManager()
        ws_a = DummyWebSocket()
        ws_b = DummyWebSocket()

        await cm.connect(ws_a, "tenant-a")
        await cm.connect(ws_b, "tenant-b")
        payload = {"type": "job_assigned", "job_id": "job-1", "technician_id": "tech-1"}

        await cm.broadcast_to_tenant("tenant-a", payload)

        assert ws_a.sent == [payload]
        assert ws_b.sent == []

    asyncio.run(_run())


def test_job_assigned_broadcast() -> None:
    async def _run() -> None:
        tenant_id = "tenant-a"
        listener = DummyWebSocket()
        await manager.connect(listener, tenant_id)

        sender = DummyWebSocket(
            incoming=[{"type": "job_assigned", "job_id": "job-123", "technician_id": "tech-42"}]
        )
        await dispatch_board_ws(sender, token=make_token(tenant_id))

        expected = {"type": "job_assigned", "job_id": "job-123", "technician_id": "tech-42"}
        assert sender.sent == [expected]
        assert listener.sent == [expected]

    asyncio.run(_run())


def test_disconnect_cleanup() -> None:
    async def _run() -> None:
        tenant_id = "tenant-cleanup"
        ws = DummyWebSocket()

        await dispatch_board_ws(ws, token=make_token(tenant_id))

        assert tenant_id not in manager._connections

    asyncio.run(_run())


def test_invalid_token_rejected() -> None:
    async def _run() -> None:
        ws = DummyWebSocket()
        await dispatch_board_ws(ws, token="not-a-token")
        assert ws.accepted is False
        assert ws.closed_code == 1008

    asyncio.run(_run())


def test_multiple_clients() -> None:
    async def _run() -> None:
        cm = ConnectionManager()
        ws_1 = DummyWebSocket()
        ws_2 = DummyWebSocket()
        ws_3 = DummyWebSocket()

        await cm.connect(ws_1, "tenant-shared")
        await cm.connect(ws_2, "tenant-shared")
        await cm.connect(ws_3, "tenant-shared")

        payload = {"type": "board_refresh"}
        await cm.broadcast_to_tenant("tenant-shared", payload)

        assert ws_1.sent == [payload]
        assert ws_2.sent == [payload]
        assert ws_3.sent == [payload]

    asyncio.run(_run())


def test_job_status_changed_broadcast() -> None:
    async def _run() -> None:
        tenant_id = "tenant-a"
        listener = DummyWebSocket()
        await manager.connect(listener, tenant_id)

        sender = DummyWebSocket(incoming=[{"type": "job_status_changed", "job_id": "job-9", "status": "done"}])
        await dispatch_board_ws(sender, token=make_token(tenant_id))

        expected = {"type": "job_status", "job_id": "job-9", "status": "done"}
        assert sender.sent == [expected]
        assert listener.sent == [expected]

    asyncio.run(_run())


def test_technician_location_broadcast() -> None:
    async def _run() -> None:
        tenant_id = "tenant-a"
        listener = DummyWebSocket()
        await manager.connect(listener, tenant_id)

        sender = DummyWebSocket(
            incoming=[{"type": "technician_location", "tech_id": "tech-1", "lat": 41.88, "lng": -87.63}]
        )
        await dispatch_board_ws(sender, token=make_token(tenant_id))

        expected = {"type": "tech_location", "tech_id": "tech-1", "lat": 41.88, "lng": -87.63}
        assert sender.sent == [expected]
        assert listener.sent == [expected]

    asyncio.run(_run())

def test_send_to_user_delivers_only_to_user() -> None:
    """send_to_user must not broadcast tenant-wide — per-user leak regression guard."""
    async def _run() -> None:
        cm = ConnectionManager()
        ws_alice = DummyWebSocket()
        ws_bob = DummyWebSocket()
        ws_alice_other_tab = DummyWebSocket()

        await cm.connect(ws_alice, "tenant-a", user_id="alice")
        await cm.connect(ws_alice_other_tab, "tenant-a", user_id="alice")
        await cm.connect(ws_bob, "tenant-a", user_id="bob")

        payload = {"type": "notification", "body": "private message for alice"}
        await cm.send_to_user("tenant-a", "alice", payload)

        assert ws_alice.sent == [payload], "alice should receive her notification"
        assert ws_alice_other_tab.sent == [payload], "alice's other tab should also receive"
        assert ws_bob.sent == [], "bob must NOT receive alice's private notification"

    asyncio.run(_run())


def test_send_to_user_missing_user_is_silent() -> None:
    """send_to_user to a disconnected user silently drops; no error."""
    async def _run() -> None:
        cm = ConnectionManager()
        ws = DummyWebSocket()
        await cm.connect(ws, "tenant-a", user_id="alice")

        # Send to a user who has no registered socket
        await cm.send_to_user("tenant-a", "ghost", {"type": "notification"})
        assert ws.sent == []

    asyncio.run(_run())


def test_send_to_user_does_not_cross_tenants() -> None:
    """send_to_user scoped by (tenant, user) — same user_id in different tenants must not leak."""
    async def _run() -> None:
        cm = ConnectionManager()
        ws_a = DummyWebSocket()
        ws_b = DummyWebSocket()

        await cm.connect(ws_a, "tenant-a", user_id="shared-user-id")
        await cm.connect(ws_b, "tenant-b", user_id="shared-user-id")

        await cm.send_to_user("tenant-a", "shared-user-id", {"type": "n"})

        assert ws_a.sent == [{"type": "n"}]
        assert ws_b.sent == []

    asyncio.run(_run())
