from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.modules import _load_user_permissions, decode_live_access_token, require_module
from gdx_dispatch.core.permissions import WILDCARD
from gdx_dispatch.core.tenant import single_tenant
from gdx_dispatch.core.websocket import manager
from gdx_dispatch.routers.auth.core import finalize_login_jwt

log = logging.getLogger(__name__)

router = APIRouter(tags=["dispatch-ws"], dependencies=[Depends(require_module("dispatch"))])

# The keys the REST board already gates on (GDXA-1210): every
# /api/dispatch/* read is jobs.read_all, and assigning a job is jobs.write.
_READ_KEY = "jobs.read_all"
_SEND_KEY = "jobs.write"

# An open socket re-runs the handshake's gate on every frame, at least this
# often, and at its token's exp (GDXA-1440): a revoked token or a deleted,
# deactivated or demoted user loses the board within this many seconds, an
# expired token at its exp. Without it a socket outlived both for as long as
# the tab stayed open.
_RECHECK_SECONDS = 30.0
_MIN_WAKE_SECONDS = 0.05


def _board_permissions(websocket: WebSocket, claims: dict[str, Any]) -> set[str]:
    """The token holder's permission set, admitted and resolved as a REST
    request is: ``finalize_login_jwt`` (the gate stack behind
    ``get_current_user``: no users row, deleted, inactive, wrong tenant or
    must-change-password raise; users.role overrides the claim), then the
    role snapshot. Raises HTTPException where REST would refuse.

    TenantMiddleware is HTTP-only, so the socket's state carries no tenant;
    pin it the way the middleware pins every request.
    """
    if not getattr(websocket.state, "tenant", None):
        websocket.state.tenant = single_tenant()
    user = finalize_login_jwt(
        websocket,
        sub=str(claims.get("sub", "")),
        tenant_claim=str(claims.get("tenant_id", "")),
        role=str(claims.get("role", "user")),
        actor_kind="human",
        jti=claims.get("jti"),
    )
    with SessionLocal() as db:
        return _load_user_permissions(db, websocket, user)


def _holds(perms: set[str], key: str) -> bool:
    return WILDCARD in perms or key in perms


def _decode_token(websocket: WebSocket, token: str) -> dict[str, Any] | None:
    """Claims of ``token`` if it is a live access token, else None.

    The same check as the bearer fallback in core/modules.py: signature and
    expiry, ``typ`` absent or ``"access"``, and the ``jti`` not on the
    revocation denylist (GDXA-1188). Raises HTTPException(503) when the
    denylist check itself fails. Run at the handshake, and by ``_recheck``.
    """
    return decode_live_access_token(token, websocket) or None


def _recheck(websocket: WebSocket, token: str) -> set[str] | None:
    """The handshake's whole gate again, for a socket already open
    (GDXA-1440): the token, then the user behind it. Permissions when the
    holder may still read the board, else None.

    Both halves matter. Logout and /auth/admin/revoke put the access token on
    the denylist; deleting, deactivating or demoting a user revokes only the
    refresh tokens, so only ``_board_permissions`` sees those. Sync, for the
    threadpool: the denylist may call Redis and the permissions read the DB.
    Raises HTTPException(5xx) when the denylist check fails. A database
    outage does not raise: ``_db_verify_user`` treats it as no users row, so
    it reads as not admitted (1008), failing closed.
    """
    claims = _decode_token(websocket, token)
    if not claims:
        return None
    try:
        perms = _board_permissions(websocket, claims)
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        return None
    return perms if _holds(perms, _READ_KEY) else None


def _next_wake(claims: dict[str, Any]) -> float:
    """Seconds until the receive loop next re-checks the token: the recheck
    interval, or sooner if the token expires first."""
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return _RECHECK_SECONDS
    return max(_MIN_WAKE_SECONDS, min(_RECHECK_SECONDS, exp - time.time()))


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
        # Every close below comes before accept, and uvicorn answers any
        # pre-accept close with HTTP 403, so a client cannot tell 1011 from
        # 1008; only TestClient surfaces the code. Only the server log tells a
        # broken revocation check from a refusal: decode_live_access_token's
        # traceback, then this line.
        log.warning("dispatch_ws_handshake_closed_1011: token revocation check unavailable")
        await websocket.close(code=1011)
        return
    tenant_id = str(payload.get("tenant_id", "")).strip() if payload else ""
    user_id = str(payload.get("sub", "")).strip() if payload else ""
    if not tenant_id:
        await websocket.close(code=1008)
        return
    # A tenant-bearing token is not enough: a customer or technician token
    # carries tenant_id too. Admit only who may read the board over REST.
    try:
        perms = await run_in_threadpool(_board_permissions, websocket, payload)
    except HTTPException as exc:
        await websocket.close(code=1011 if exc.status_code >= 500 else 1008)
        return
    if not _holds(perms, _READ_KEY):
        await websocket.close(code=1008)
        return
    may_send = _holds(perms, _SEND_KEY)

    await manager.connect(websocket, tenant_id, user_id or None)

    try:
        while True:
            try:
                incoming = await asyncio.wait_for(websocket.receive_json(), timeout=_next_wake(payload))
            except TimeoutError:
                # No frame before the wake: re-check an idle socket anyway.
                log.debug("dispatch_ws_idle_recheck sub=%s", user_id)
                incoming = None
            # Re-check before acting on the frame, so a revoked holder's last
            # frame is not rebroadcast. After accept the close code reaches
            # the client, unlike the handshake's.
            try:
                perms = await run_in_threadpool(_recheck, websocket, token)
            except HTTPException:
                log.warning("dispatch_ws_open_closed_1011: token or user check unavailable")
                await websocket.close(code=1011)
                await manager.disconnect(websocket, tenant_id)
                return
            if perms is None:
                log.info("dispatch_ws_holder_no_longer_admitted sub=%s", user_id)
                await websocket.close(code=1008, reason="token or account no longer admitted")
                await manager.disconnect(websocket, tenant_id)
                return
            may_send = _holds(perms, _SEND_KEY)
            if not isinstance(incoming, dict) or not may_send:
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
