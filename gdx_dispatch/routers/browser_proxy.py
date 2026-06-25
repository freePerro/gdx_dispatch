"""Core proxy for the plugin browser-stream (ADR-014).

Auth is split so the WebSocket never hand-rolls authorization (which would skip
the real gate stack — revocation denylist, DB user-verify, DB role overlay,
tenant match — and reopen known bypass classes):

  * POST /api/plugins/_browser/ticket — HTTP, so it runs the FULL `get_current_user`
    gate stack. It then enforces owner role, that the plugin *currently* declares
    the "browser" permission, recorded owner consent, and the URL allowlist. On
    success it mints a short-lived signed ticket bound to (plugin key, url).
  * WS /api/plugins/_browser/ws?ticket=... — validates only the ticket (signature
    + expiry + scope), re-checks the allowlist, then relays frames/input to the
    internal plugin-host stream. No role/consent logic lives in the socket.

This way a revoked or DB-demoted owner cannot open a stream with a stale token,
and a stale consent row for a plugin that no longer declares "browser" is
rejected at ticket time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import quote

import jwt
import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from jwt.exceptions import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.plugin_consent import fetch_permissions, has_permission_consent
from gdx_dispatch.plugin_host.browser_stream import host_allowed
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

PRIV = os.getenv("RS_PRIVATE_KEY", "").replace("\\n", "\n").strip()
PUB = os.getenv("RS_PUBLIC_KEY", "").replace("\\n", "\n").strip()
ALG = "RS256" if PRIV else "HS256"
SIGN_KEY = PRIV or os.getenv("JWT_SECRET", "dev-secret")
VERIFY_KEY = (PUB or PRIV) if ALG == "RS256" else SIGN_KEY

_OWNER_ROLES = {"owner", "superadmin"}
_TICKET_TTL = 30  # seconds — just long enough to open the socket
_SCOPE = "browserstream"

router = APIRouter(tags=["plugin-browser"])


class TicketReq(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2000)


@router.post("/api/plugins/_browser/ticket")
def issue_ticket(
    body: TicketReq,
    user: dict = Depends(get_current_user),  # full gate stack runs here
    db: Session = Depends(get_db),
) -> dict:
    if user.get("role") not in _OWNER_ROLES:
        raise HTTPException(403, "browser stream is owner-only")
    # Re-check the LIVE declared permission, not just a stored consent row.
    if "browser" not in fetch_permissions(body.key):
        raise HTTPException(403, f"plugin {body.key!r} does not declare the browser permission")
    if not has_permission_consent(db, body.key, "browser"):
        raise HTTPException(403, "owner consent required for the browser permission")
    if not host_allowed(body.url):
        raise HTTPException(400, "url host is not on the allowlist")
    ticket = jwt.encode(
        {
            # `typ` is deliberately NOT "access"/None so this ticket is rejected
            # by the access-token path (get_current_user) — it can't be replayed
            # as a bearer token to act as the owner.
            "typ": _SCOPE,
            "scope": _SCOPE,
            "k": body.key,
            "u": body.url,
            "sub": str(user.get("user_id") or user.get("sub") or ""),
            "exp": int(time.time()) + _TICKET_TTL,
        },
        SIGN_KEY,
        algorithm=ALG,
    )
    return {"ticket": ticket}


def _decode_ticket(ticket: str) -> dict | None:
    try:
        c = jwt.decode(ticket, VERIFY_KEY, algorithms=[ALG])
    except JWTError:
        return None
    if c.get("scope") != _SCOPE:
        return None
    return c


def _ws_host_url() -> str:
    return (
        os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000")
        .rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
    )


@router.websocket("/api/plugins/_browser/ws")
async def browser_stream_proxy(websocket: WebSocket, ticket: str = "") -> None:
    claims = _decode_ticket(ticket)
    if not claims or not host_allowed(claims.get("u", "")):
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()
    upstream = f"{_ws_host_url()}/internal/browser/ws?url={quote(claims['u'], safe='')}"
    try:
        async with websockets.connect(upstream, max_size=None) as up:
            await asyncio.gather(
                _client_to_upstream(websocket, up),
                _upstream_to_client(up, websocket),
            )
    except Exception:
        log.exception("browser-stream proxy error")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _client_to_upstream(client: WebSocket, up) -> None:
    try:
        while True:
            await up.send(await client.receive_text())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("client->upstream ended: %s", e)
    finally:
        try:
            await up.close()
        except Exception:
            pass


async def _upstream_to_client(up, client: WebSocket) -> None:
    try:
        async for msg in up:
            await client.send_text(msg if isinstance(msg, str) else msg.decode())
    except Exception as e:
        log.debug("upstream->client ended: %s", e)
