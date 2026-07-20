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

import httpx
import jwt
import websockets
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
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


def _gate_browser(user: dict, db: Session, key: str) -> None:
    """The browser-permission gate stack, shared by every _browser route:
    owner role + the plugin LIVE-declares "browser" + recorded owner consent."""
    if user.get("role") not in _OWNER_ROLES:
        raise HTTPException(403, "browser stream is owner-only")
    # Re-check the LIVE declared permission, not just a stored consent row.
    if "browser" not in fetch_permissions(key):
        raise HTTPException(403, f"plugin {key!r} does not declare the browser permission")
    if not has_permission_consent(db, key, "browser"):
        raise HTTPException(403, "owner consent required for the browser permission")


@router.post("/api/plugins/_browser/ticket")
def issue_ticket(
    body: TicketReq,
    user: dict = Depends(get_current_user),  # full gate stack runs here
    db: Session = Depends(get_db),
) -> dict:
    _gate_browser(user, db, body.key)
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


def _host_http_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


class CredsReq(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)


async def _creds_call(method: str, **kwargs) -> dict:
    """Relay a credentials op to the plugin-host's internal store."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.request(
            method, f"{_host_http_url()}/internal/browser/credentials", **kwargs
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:500])
    return r.json()


@router.post("/api/plugins/_browser/credentials")
async def save_browser_credentials(
    body: CredsReq,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Remember the sign-in for a plugin's browser workspace. Same gate stack
    as the stream ticket; stored encrypted on the plugin-host (never in core),
    and only ever autofilled into allowlisted hosts inside the stream."""
    _gate_browser(user, db, body.key)
    if not (body.username or body.password):
        raise HTTPException(400, "provide a username and/or password")
    return await _creds_call(
        "POST",
        json={"key": body.key, "username": body.username, "password": body.password},
    )


@router.get("/api/plugins/_browser/credentials")
async def browser_credentials_status(
    key: str = Query(min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Whether a remembered login exists (username + has_password flag only —
    the password itself never leaves the plugin-host)."""
    _gate_browser(user, db, key)
    return await _creds_call("GET", params={"key": key})


@router.delete("/api/plugins/_browser/credentials")
async def forget_browser_credentials(
    key: str = Query(min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Forget a plugin's remembered sign-in."""
    _gate_browser(user, db, key)
    return await _creds_call("DELETE", params={"key": key})


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
    # `k` (the plugin key) rides along so the host can reload/persist that
    # plugin's remembered login session. It came from the signed ticket, which
    # was minted only after the full auth + consent gate stack.
    upstream = (
        f"{_ws_host_url()}/internal/browser/ws"
        f"?url={quote(claims['u'], safe='')}&key={quote(str(claims.get('k', '')), safe='')}"
    )
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
