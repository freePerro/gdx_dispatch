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
import json
import logging
import os
import time
import uuid
from urllib.parse import quote

import httpx
import jwt
import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from jwt.exceptions import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_or_rollback, audit_ready_db
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
    request: Request,
    user: dict = Depends(get_current_user),  # full gate stack runs here
    db: Session = Depends(audit_ready_db),
) -> dict:
    _gate_browser(user, db, body.key)
    if not host_allowed(body.url):
        raise HTTPException(400, "url host is not on the allowlist")
    # One id for this whole session: it names the recording directory AND rides
    # in the audit row below, so "who opened a browser" and "here is the
    # recording of what they did" resolve to each other from either direction.
    sid = uuid.uuid4().hex
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
            "sid": sid,
            "exp": int(time.time()) + _TICKET_TTL,
        },
        SIGN_KEY,
        algorithm=ALG,
    )
    # Audited after the mint, so "issued" is literally true, and before the
    # return, so a ticket the trail could not record never reaches the caller.
    # A ticket is a capability to drive a browser as the tenant against that
    # host — which owner opened what, and when, is the only record of it.
    audit_or_rollback(
        db,
        action="plugin.browser_ticket_issued",
        entity_type="plugin",
        entity_id=body.key,
        actor=user,
        request=request,
        details={"key": body.key, "url": body.url, "sid": sid},
    )
    db.commit()
    return {"ticket": ticket}


def _host_http_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


class CredsReq(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)


async def _creds_call(method: str, **kwargs) -> dict:
    """Relay a credentials op to the plugin-host's internal store."""
    from gdx_dispatch.core.plugin_consent import internal_auth_headers

    headers = {**internal_auth_headers(), **kwargs.pop("headers", {})}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.request(
            method, f"{_host_http_url()}/internal/browser/credentials", headers=headers, **kwargs
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:500])
    return r.json()


@router.post("/api/plugins/_browser/credentials")
async def save_browser_credentials(
    body: CredsReq,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """Remember the sign-in for a plugin's browser workspace. Same gate stack
    as the stream ticket; stored encrypted on the plugin-host (never in core),
    and only ever autofilled into allowlisted hosts inside the stream."""
    _gate_browser(user, db, body.key)
    if not (body.username or body.password):
        raise HTTPException(400, "provide a username and/or password")
    # Audited before the remote store, and named for what is actually known at
    # this point: the credential lands on the plugin-host, which core cannot roll
    # back, so recording intent first is the only ordering that can never lose
    # the trail — but the store can still fail, so this is a REQUEST, not a fact.
    # Never record the password itself.
    audit_or_rollback(
        db,
        action="plugin.browser_credentials_save_requested",
        entity_type="plugin",
        entity_id=body.key,
        actor=user,
        request=request,
        details={"key": body.key, "username": body.username or None,
                 "password_set": bool(body.password)},
    )
    db.commit()
    return await _creds_call(
        "POST",
        json={"key": body.key, "username": body.username, "password": body.password},
    )


@router.get("/api/plugins/_browser/credentials")
async def browser_credentials_status(
    key: str = Query(min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """Whether a remembered login exists (username + has_password flag only —
    the password itself never leaves the plugin-host)."""
    _gate_browser(user, db, key)
    return await _creds_call("GET", params={"key": key})


@router.delete("/api/plugins/_browser/credentials")
async def forget_browser_credentials(
    request: Request,
    key: str = Query(min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """Forget a plugin's remembered sign-in."""
    _gate_browser(user, db, key)
    # "requested", not "forgotten": _creds_call can still fail against the
    # plugin-host, and a trail claiming a revocation that did not happen is
    # worse than one that records the attempt.
    audit_or_rollback(
        db,
        action="plugin.browser_credentials_forget_requested",
        entity_type="plugin",
        entity_id=key,
        actor=user,
        request=request,
        details={"key": key},
    )
    db.commit()
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
    # Record the session. This relay is the right place: it already sees every
    # input and every server message, it is NOT the ack-gated frame loop in the
    # plugin-host (so it cannot freeze the operator's live view), and the
    # ticket's claims are already decoded here so identity is free.
    rec = _start_recorder(claims)

    try:
        from gdx_dispatch.core.plugin_consent import internal_auth_headers

        async with websockets.connect(
            upstream, max_size=None, additional_headers=internal_auth_headers()
        ) as up:
            if rec is not None:
                await rec.start()
            await asyncio.gather(
                _client_to_upstream(websocket, up, rec),
                _upstream_to_client(up, websocket, rec),
                _recorder_heartbeat(websocket, rec),
            )
    except Exception:
        log.exception("browser-stream proxy error")
    finally:
        # Synchronous drain FIRST: this finally is reached by CancelledError on
        # every prod deploy (uvicorn SIGTERM), and inside a cancelled finally the
        # next await re-raises immediately — anything after it would be skipped.
        if rec is not None:
            stats = rec.close_sync("stream ended")
            _audit_session_close(claims, stats)
        try:
            await websocket.close()
        except Exception:
            pass


def _start_recorder(claims: dict):
    """Build a recorder for this session, or None. Never raises."""
    if os.getenv("GDX_SESSION_RECORDING", "1").lower() in ("0", "false", "no"):
        return None
    try:
        from gdx_dispatch.core.session_recorder import SessionRecorder

        return SessionRecorder(
            actor=str(claims.get("sub") or ""),
            plugin_key=str(claims.get("k") or ""),
            url=str(claims.get("u") or ""),
            session_id=str(claims.get("sid") or "") or None,
        )
    except Exception as e:
        log.warning("session recorder unavailable: %s", e)
        return None


async def _recorder_heartbeat(client: WebSocket, rec) -> None:
    """Push real recorder stats to the UI every 5s.

    Deliberately driven by bytes actually written, not by a client-side boolean:
    a badge that says "recording" whether or not a byte hit disk is decoration,
    not evidence. Counterfactual: chmod 000 the recording dir mid-session and
    this must report degraded within 5s.
    """
    if rec is None:
        return
    try:
        while True:
            await asyncio.sleep(5)
            await client.send_text(json.dumps({"type": "rec", **rec.stats()}))
    except Exception as e:
        log.debug("recorder heartbeat ended: %s", e)


def _audit_session_close(claims: dict, stats: dict) -> None:
    """Pair the audited ticket issuance with an audited "what was done with it".

    Invariant #1 says a state-changing action answers who/what/when. Issuing the
    browser capability was already audited; until now, what happened inside the
    session was not.
    """
    try:
        # The sync variant deliberately: this runs from a finally block that a
        # deploy-time SIGTERM reaches by cancellation, where an await would
        # re-raise and skip the row entirely.
        from gdx_dispatch.core.audit import log_audit_event_sync
        from gdx_dispatch.core.database import SessionLocal

        db = SessionLocal()
        try:
            log_audit_event_sync(
                db,
                action="plugin.browser_session_closed",
                entity_type="plugin",
                entity_id=str(claims.get("k") or ""),
                user_id=str(claims.get("sub") or "") or None,
                details=stats,
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.warning("session-close audit failed: %s", e)


async def _client_to_upstream(client: WebSocket, up, rec=None) -> None:
    try:
        while True:
            raw = await client.receive_text()
            if rec is not None:
                rec.note_client(raw)  # never raises, never blocks
            await up.send(raw)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("client->upstream ended: %s", e)
    finally:
        try:
            await up.close()
        except Exception:
            pass


async def _upstream_to_client(up, client: WebSocket, rec=None) -> None:
    try:
        async for msg in up:
            text = msg if isinstance(msg, str) else msg.decode()
            if rec is not None:
                rec.note_server(text)  # never raises, never blocks
            await client.send_text(text)
    except Exception as e:
        log.debug("upstream->client ended: %s", e)
