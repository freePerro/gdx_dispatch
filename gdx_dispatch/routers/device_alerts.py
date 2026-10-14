"""Office alerts on the Android app — the bell feed for a phone's background service.

The app's foreground service cannot share the browser session (refresh tokens rotate with replay
detection), so an office user who turns on "Alerts on this phone" gets an
opaque device token. Only its SHA-256 is stored, in
``device_alert_registrations``; nothing but these three routes looks it up, so
it is no login anywhere else.

- ``POST /api/notifications/device-alerts/token`` — signed-in ``nav.office``
  user mints a token (returned once).
- ``POST /api/notifications/device-alerts/revoke`` — the device token revokes
  itself; works after the app session expired.
- ``GET /api/notifications/device-alerts/feed`` — long-poll of unread bell rows.

Every feed request re-checks the token, its age, the user (active, not
deleted) and ``nav.office`` against the database, so deactivation and role
demotion end delivery without a revoke list. The phone treats only a 401 as
final: the module and permission checks answer a database error as "off", so
a 403 may be a blip and is retried.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_or_rollback
from gdx_dispatch.core.database import SessionLocal, get_db
from gdx_dispatch.core.modules import (
    _load_user_permissions,
    is_module_enabled,
    require_module,
    require_permission,
)
from gdx_dispatch.models.tenant_models import DeviceAlertRegistration, Notification, User
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(tags=["notifications"])

TOKEN_HEADER = "X-Device-Alerts-Token"  # noqa: S105  (a header name, not a secret)
TOKEN_MAX_AGE = timedelta(days=90)
# A bell row committed a little after the cursor was taken still carries a
# created_at before it. Every response re-sends this much history and the
# phone drops ids it has already shown.
CURSOR_OVERLAP = timedelta(minutes=2)
POLL_INTERVAL_SECONDS = 3.0
MAX_WAIT_SECONDS = 25
MAX_ITEMS = 20
# A full response means more may be waiting. The overlap would re-send the
# rows just shown and they would fill every later response, so a full page
# hands back a keyset cursor instead: after exactly the last row sent.
PAGE_PREFIX = "p:"
LAST_SEEN_EVERY = timedelta(minutes=1)


def feed_session_factory() -> Callable[[], Session]:
    """Sessions for the feed. A dependency so tests can point it at their DB.

    The feed opens one short session per check instead of ``Depends(get_db)``,
    which would hold a connection for the whole long-poll.
    """
    return SessionLocal


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    # SQLite hands DateTime(timezone=True) back naive; it was stored as UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


class DeviceAlertsTokenRequest(BaseModel):
    device_label: str | None = Field(default=None, max_length=200)


class DeviceAlertsTokenResponse(BaseModel):
    token: str
    registration_id: str
    # Whose alerts these are: the phone turns them off when someone else signs in.
    user_id: str


class DeviceAlertsRevokeRequest(BaseModel):
    reason: str = Field(default="disabled", pattern="^(disabled|logout|rotated|user_changed)$")


class DeviceAlertItem(BaseModel):
    id: str
    title: str
    message: str
    category: str
    created_at: str


class DeviceAlertsFeedResponse(BaseModel):
    cursor: str
    items: list[DeviceAlertItem]


@router.post(
    "/api/notifications/device-alerts/token",
    response_model=DeviceAlertsTokenResponse,
    dependencies=[Depends(require_module("communications")), Depends(require_permission("nav.office"))],
)
def create_device_alerts_token(
    payload: DeviceAlertsTokenRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeviceAlertsTokenResponse:
    """Turn on office alerts for one phone. Office users only."""
    user_id = str(current_user.get("sub") or current_user.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not signed in")
    token = secrets.token_urlsafe(32)
    row = DeviceAlertRegistration(
        id=str(uuid4()),
        user_id=user_id,
        token_hash=_hash(token),
        device_label=(payload.device_label or "").strip() or None,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    audit_or_rollback(
        db,
        action="device_alerts_enabled",
        entity_type="device_alert_registration",
        entity_id=row.id,
        actor=current_user,
        request=request,
        details={"device_label": row.device_label},
    )
    db.commit()
    return DeviceAlertsTokenResponse(token=token, registration_id=row.id, user_id=user_id)


@router.post("/api/notifications/device-alerts/revoke")
def revoke_device_alerts_token(
    request: Request,
    payload: DeviceAlertsRevokeRequest | None = None,
    token: str = Header(default="", alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    """The phone turns its own alerts off. Idempotent for a known token."""
    row = _registration(db, token)
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown device token")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        row.revoked_reason = (payload or DeviceAlertsRevokeRequest()).reason
        db.flush()
        audit_or_rollback(
            db,
            action="device_alerts_disabled",
            entity_type="device_alert_registration",
            entity_id=row.id,
            actor={"sub": row.user_id},
            request=request,
            details={"reason": row.revoked_reason, "device_label": row.device_label},
        )
        db.commit()
    return {"revoked": True}


def _registration(db: Session, token: str) -> DeviceAlertRegistration | None:
    if not token:
        return None
    return db.execute(
        select(DeviceAlertRegistration).where(DeviceAlertRegistration.token_hash == _hash(token))
    ).scalar_one_or_none()


def _gate(db: Session, request: Request, token: str) -> str:
    """The token's user id, or the HTTP error that ends delivery."""
    row = _registration(db, token)
    if row is None or row.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Phone alerts are off for this device")
    now = datetime.now(UTC)
    if now - _aware(row.created_at) > TOKEN_MAX_AGE:
        raise HTTPException(status_code=401, detail="Phone alerts token expired")
    try:
        user_uuid = UUID(row.user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Phone alerts token has no user") from None
    user = db.get(User, user_uuid)
    if user is None or user.deleted_at is not None or user.active is False:
        raise HTTPException(status_code=401, detail="User is not active")
    if not is_module_enabled("communications", request, db):
        raise HTTPException(status_code=403, detail="Module 'communications' is not enabled")
    from gdx_dispatch.core.permissions import WILDCARD

    # The role comes from the users row just read, never from a token claim.
    perms = _load_user_permissions(db, request, {"sub": row.user_id, "role": user.role})
    if WILDCARD not in perms and "nav.office" not in perms:
        raise HTTPException(status_code=403, detail="Phone alerts are for office users")
    seen = _aware(row.last_seen_at)
    if seen is None or now - seen > LAST_SEEN_EVERY:
        row.last_seen_at = now
        db.commit()
    return row.user_id


def _filters(tenant_id: str, user_id: str, after: Any) -> list[Any]:
    """``after`` is an ISO time (rows newer than it) or a ``(created_at, id)``
    keyset from a full page (rows strictly after that row)."""
    if isinstance(after, tuple):
        created, row_id = after
        newer = or_(
            Notification.created_at > created,
            and_(Notification.created_at == created, Notification.id > row_id),
        )
    else:
        newer = Notification.created_at > after
    return [
        Notification.tenant_id == tenant_id,
        or_(Notification.user_id == user_id, Notification.user_id.is_(None)),
        Notification.is_read == 0,
        Notification.deleted_at.is_(None),
        newer,
    ]


def _has_newer(factory: Callable[[], Session], tenant_id: str, user_id: str, after: Any) -> bool:
    with factory() as db:
        return (
            db.execute(
                select(Notification.id).where(*_filters(tenant_id, user_id, after)).limit(1)
            ).first()
            is not None
        )


def _window(
    factory: Callable[[], Session], tenant_id: str, user_id: str, after: Any
) -> tuple[str, list[DeviceAlertItem]]:
    now_iso = datetime.now(UTC).isoformat()
    if not isinstance(after, tuple):
        after = (after - CURSOR_OVERLAP).isoformat()
    with factory() as db:
        rows = db.execute(
            select(Notification)
            .where(*_filters(tenant_id, user_id, after))
            .order_by(Notification.created_at.asc(), Notification.id.asc())
            .limit(MAX_ITEMS)
        ).scalars().all()
        items = [
            DeviceAlertItem(
                id=str(r.id),
                title=str(r.title),
                message=str(r.message),
                category=str(r.category),
                created_at=str(r.created_at),
            )
            for r in rows
        ]
    if len(rows) == MAX_ITEMS:
        last = rows[-1]
        return f"{PAGE_PREFIX}{last.created_at}|{last.id}", items
    return now_iso, items


def _parse_cursor(after: str) -> Any:
    if after.startswith(PAGE_PREFIX):
        created, sep, row_id = after[len(PAGE_PREFIX):].rpartition("|")
        if not sep or not created or not row_id:
            raise ValueError(after)
        datetime.fromisoformat(created)  # shape check only; compared as stored
        return (created, row_id)
    return _aware(datetime.fromisoformat(after)).astimezone(UTC)


@router.get("/api/notifications/device-alerts/feed", response_model=DeviceAlertsFeedResponse)
async def device_alerts_feed(
    request: Request,
    after: str | None = Query(default=None, max_length=128),
    wait: int = Query(default=0, ge=0, le=MAX_WAIT_SECONDS),
    token: str = Header(default="", alias=TOKEN_HEADER),
    factory: Callable[[], Session] = Depends(feed_session_factory),
) -> DeviceAlertsFeedResponse:
    """Unread bell rows for the token's user, newer than ``after``.

    With no ``after`` it returns no items and the current cursor, so turning
    alerts on never replays the backlog. Otherwise it waits up to ``wait``
    seconds for a row newer than the cursor, then returns every unread row
    from two minutes before the cursor on (the phone dedupes by id). A full
    response returns a keyset cursor, so a burst bigger than one response
    arrives over the next few polls instead of losing its tail.
    """
    tenant_id = _tenant_id(request)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")

    def _check() -> str:
        with factory() as db:
            return _gate(db, request, token)

    user_id = await asyncio.to_thread(_check)
    if not after:
        return DeviceAlertsFeedResponse(cursor=datetime.now(UTC).isoformat(), items=[])
    try:
        cursor = _parse_cursor(after)
    except ValueError:
        raise HTTPException(status_code=422, detail="Bad cursor") from None
    newer_than = cursor if isinstance(cursor, tuple) else cursor.isoformat()

    deadline = asyncio.get_running_loop().time() + wait
    while not await asyncio.to_thread(_has_newer, factory, tenant_id, user_id, newer_than):
        if asyncio.get_running_loop().time() >= deadline or await request.is_disconnected():
            break
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    next_cursor, items = await asyncio.to_thread(_window, factory, tenant_id, user_id, cursor)
    return DeviceAlertsFeedResponse(cursor=next_cursor, items=items)
