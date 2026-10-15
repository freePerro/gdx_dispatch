from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from gdx_dispatch.core.modules import decode_live_access_token, require_module
from gdx_dispatch.core.websocket import manager

log = logging.getLogger(__name__)

router = APIRouter(tags=["dispatch-ws"], dependencies=[Depends(require_module("dispatch"))])


def _decode_token(websocket: WebSocket, token: str) -> dict[str, Any] | None:
    """Claims of ``token`` if it is a live access token, else None.

    The same check as the bearer fallback in core/modules.py: signature and
    expiry, ``typ`` absent or ``"access"``, and the ``jti`` not on the
    revocation denylist (GDXA-1188). Raises HTTPException(503) when the
    denylist check itself fails. Checked at the handshake only: a socket
    already open when its token is revoked is not re-checked.
    """
    return decode_live_access_token(token, websocket) or None


def _normalize_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    msg_type = str(payload.get("type", "")).strip()

    if msg_type == "job_assigned":
        return {
            "type": "job_assigned",
            "job_id": payload.get("job_id"),
            "technician_id": payload.get("technician_id"),
        }

    if msg_type == "job_status_changed":
        return {
            "type": "job_status",
            "job_id": payload.get("job_id"),
            "status": payload.get("status"),
        }

    if msg_type == "technician_location":
        return {
            "type": "tech_location",
            "tech_id": payload.get("tech_id"),
            "lat": payload.get("lat"),
            "lng": payload.get("lng"),
        }

    if msg_type == "board_refresh":
        return {"type": "board_refresh"}

    return None


@router.websocket("/ws/dispatch")
async def dispatch_board_ws(websocket: WebSocket, token: str = "") -> None:
    try:
        payload = _decode_token(websocket, token)
    except HTTPException:
        await websocket.close(code=1011)
        return
    tenant_id = str(payload.get("tenant_id", "")).strip() if payload else ""
    user_id = str(payload.get("sub", "")).strip() if payload else ""
    if not tenant_id:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, tenant_id, user_id or None)

    try:
        while True:
            incoming = await websocket.receive_json()
            if not isinstance(incoming, dict):
                continue
            outgoing = _normalize_message(incoming)
            if outgoing is None:
                continue
            await manager.broadcast_to_tenant(tenant_id, outgoing)
    except WebSocketDisconnect:
        log.exception("dispatch_ws_client_disconnected")
        await manager.disconnect(websocket, tenant_id)
    except (RuntimeError, ValueError):
        log.exception("dispatch_ws_failed", extra={"tenant_id": tenant_id})
        await manager.disconnect(websocket, tenant_id)
