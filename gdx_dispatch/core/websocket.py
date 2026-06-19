from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Track and broadcast active websocket connections per tenant + user."""

    def __init__(self) -> None:
        # Tenant-wide socket set (for broadcast_to_tenant)
        self._connections: dict[str, set[WebSocket]] = {}
        # Per-user socket set (for send_to_user). Keyed (tenant_id, user_id).
        # Without this, send_to_user would fall back to tenant-wide broadcast,
        # leaking per-user notifications to every user in the tenant.
        self._user_connections: dict[tuple[str, str], set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    @property
    def active_connections(self) -> dict[str, list[WebSocket]]:
        return {tenant_id: list(sockets) for tenant_id, sockets in self._connections.items()}

    @active_connections.setter
    def active_connections(self, value: dict[str, list[WebSocket]]) -> None:
        if not value:
            self._connections.clear()
            self._user_connections.clear()

    async def connect(self, websocket: WebSocket, tenant_id: str, user_id: str | None = None) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(tenant_id, set()).add(websocket)
            if user_id:
                self._user_connections.setdefault((tenant_id, user_id), set()).add(websocket)

    async def disconnect(self, websocket: WebSocket, tenant_id: str, user_id: str | None = None) -> None:
        async with self._lock:
            sockets = self._connections.get(tenant_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._connections.pop(tenant_id, None)
            if user_id:
                user_sockets = self._user_connections.get((tenant_id, user_id))
                if user_sockets:
                    user_sockets.discard(websocket)
                    if not user_sockets:
                        self._user_connections.pop((tenant_id, user_id), None)

    async def broadcast_to_tenant(self, tenant_id: str, message: dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self._connections.get(tenant_id, set()))

        stale: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                logging.getLogger(__name__).exception("broadcast_to_tenant caught exception")
                stale.append(ws)

        for ws in stale:
            await self.disconnect(ws, tenant_id)

    async def broadcast(self, tenant_id: str, message: dict[str, Any]) -> None:
        await self.broadcast_to_tenant(tenant_id, message)

    async def send_personal(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        await websocket.send_json(message)

    async def send_to_user(self, tenant_id: str, user_id: str, message: dict[str, Any]) -> None:
        """Deliver a message only to the specified user's sockets within a tenant.

        If the user has no registered sockets, the message is silently dropped
        (they will see it on next page load via REST). Previously this method
        broadcast tenant-wide, which leaked per-user notifications to every
        active tab in the tenant.
        """
        async with self._lock:
            sockets = list(self._user_connections.get((tenant_id, user_id), set()))

        if not sockets:
            return

        stale: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                logging.getLogger(__name__).exception("send_to_user caught exception")
                stale.append(ws)

        for ws in stale:
            await self.disconnect(ws, tenant_id, user_id)


manager = ConnectionManager()
