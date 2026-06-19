from __future__ import annotations

import logging
import os
from typing import Any

import jwt
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from jwt.exceptions import InvalidTokenError as JWTError

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.websocket import manager

log = logging.getLogger(__name__)

PRIV = os.getenv("RS_PRIVATE_KEY", "").replace("\\n", "\n").strip()
PUB = os.getenv("RS_PUBLIC_KEY", "").replace("\\n", "\n").strip()
ALG = "RS256" if PRIV else "HS256"
SIGN_KEY = PRIV or os.getenv("JWT_SECRET", "dev-secret")
VERIFY_KEY = (PUB or PRIV) if ALG == "RS256" else SIGN_KEY

router = APIRouter(tags=["dispatch-ws"], dependencies=[Depends(require_module("dispatch"))])


def _decode_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, VERIFY_KEY, algorithms=[ALG])
        if payload.get("typ") not in (None, "access"):
            return None
        return payload
    except JWTError:  # returns None if token is invalid or type is incorrect
        log.exception("dispatch_ws_token_decode_failed")
        return None


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
    payload = _decode_token(token)
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
