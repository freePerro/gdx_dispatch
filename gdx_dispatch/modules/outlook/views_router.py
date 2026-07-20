"""Sprint Outlook Integration — Phase 5 read-view router.

Four endpoints, every one filters through ``visibility.can_view``:

- ``GET /api/outlook/messages`` — unified inbox for the current user.
- ``GET /api/outlook/messages/by-customer/{customer_id}`` — Email tab on
  customer detail page.
- ``GET /api/outlook/messages/by-job/{job_id}`` — Email tab on job detail.
- ``GET /api/outlook/messages/{message_id}`` — single-message detail.
- ``POST /api/outlook/messages/{message_id}/personal`` — owner-only toggle of
  the per-message ``is_personal`` privacy override.

All require the ``email`` module gate + an authed user. ALL row
visibility is enforced server-side via ``visibility.filter_visible``.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook.models import OutlookMessage
from gdx_dispatch.modules.outlook.visibility import can_view, filter_visible, mailbox_owner_id
from gdx_dispatch.routers.auth import get_current_user

# D1 body live-fetch — imported lazily inside the handler to keep module load
# cheap and avoid a Graph/httpx import on every views_router import.


log = logging.getLogger("gdx_dispatch.modules.outlook.views_router")

router = APIRouter(
    prefix="/api/outlook",
    tags=["outlook", "views"],
)


# ── pydantic shapes ─────────────────────────────────────────────────────


class MessageOut(BaseModel):
    id: UUID
    subject: str | None = None
    from_address: str | None = None
    to_addresses: list[str] | None = None
    direction: str
    sent_at: str | None = None
    received_at: str | None = None
    body_preview: str | None = None
    is_read: bool
    has_attachments: bool
    linked_customer_id: UUID | None = None
    linked_job_id: UUID | None = None
    tag_strategy: str | None = None
    is_personal: bool


class MessageDetailOut(MessageOut):
    cc_addresses: list[str] | None = None
    bcc_addresses: list[str] | None = None
    conversation_id: str | None = None
    internet_message_id: str | None = None
    body_r2_key: str | None = None
    # True when the CURRENT VIEWER owns the mailbox this message belongs to.
    # Drives owner-only UI affordances (the "mark personal" toggle) without a
    # second round-trip. List serialization always leaves it False — only the
    # detail endpoint computes it.
    viewer_is_owner: bool = False


class PersonalIn(BaseModel):
    is_personal: bool


class MessageBodyOut(BaseModel):
    """Live-fetched full body for one message (D1).

    ``fetched`` is False when the Graph fetch could not run (mailbox needs
    reconnect, message gone from Graph, no account) — the caller then falls
    back to ``body_preview`` and shows ``reason``. ``body_html`` is the RAW
    Graph body; the frontend MUST render it in a sandboxed iframe (never
    v-html), because it is attacker-controlled HTML.
    """
    fetched: bool
    content_type: str | None = None  # "html" | "text"
    body_html: str | None = None
    body_preview: str | None = None
    reason: str | None = None  # populated when fetched is False


def _to_out(m: OutlookMessage) -> MessageOut:
    return MessageOut(
        id=m.id,
        subject=m.subject,
        from_address=m.from_address,
        to_addresses=m.to_addresses,
        direction=m.direction,
        sent_at=m.sent_at.isoformat() if m.sent_at else None,
        received_at=m.received_at.isoformat() if m.received_at else None,
        body_preview=m.body_preview,
        is_read=m.is_read,
        has_attachments=m.has_attachments,
        linked_customer_id=m.linked_customer_id,
        linked_job_id=m.linked_job_id,
        tag_strategy=m.tag_strategy,
        is_personal=m.is_personal,
    )


def _to_detail(m: OutlookMessage, *, viewer_is_owner: bool = False) -> MessageDetailOut:
    base = _to_out(m).model_dump()
    return MessageDetailOut(
        **base,
        cc_addresses=m.cc_addresses,
        bcc_addresses=m.bcc_addresses,
        conversation_id=m.conversation_id,
        internet_message_id=m.internet_message_id,
        body_r2_key=m.body_r2_key,
        viewer_is_owner=viewer_is_owner,
    )


def _viewer_owns_mailbox(tenant_db: Session, msg: OutlookMessage, uid: UUID) -> bool:
    """True when `uid` owns the OutlookAccount this message was synced from.

    Delegates to visibility.mailbox_owner_id — ONE owner-resolution codepath
    (string-compared: OutlookAccount.user_id is String(36) and
    `UUID('abc…') == 'abc…'` is False in Python).
    """
    owner = mailbox_owner_id(msg, tenant_db)
    return owner is not None and owner == str(uid)


# ── auth helpers ────────────────────────────────────────────────────────


def get_user_for_views(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return user


def get_db_for_views(db: Session = Depends(get_db)) -> Session:
    return db


def _user_id(user: dict[str, Any]) -> UUID:
    raw = user.get("user_id") or user.get("id") or user.get("sub")
    if not raw:
        raise HTTPException(status_code=400, detail="missing user context")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _user_role(user: dict[str, Any]) -> str:
    return (user.get("role") or "viewer").lower()


def _load_tech_emails(tenant_db: Session) -> set[str]:
    """One-shot load of all known-tech mailbox addresses for the tenant. Used
    by the visibility chokepoint's "tech recipient → all techs see" rule.
    Empty set when the User model is unavailable (test envs)."""
    try:
        from gdx_dispatch.models.tenant_models import User
        rows = (
            tenant_db.query(User)
            .filter(User.role.in_(["technician", "tech"]), User.deleted_at.is_(None))
            .all()
        )
        return {r.email.lower().strip() for r in rows if r.email}
    except Exception:  # noqa: BLE001
        # Don't crash the request — but log loudly so a broken User model
        # query doesn't silently disable the "tech recipient → all techs"
        # visibility rule.
        log.exception("views_router: _load_tech_emails failed — visibility rule degraded")
        return set()


# ── endpoints ───────────────────────────────────────────────────────────


@router.get(
    "/messages",
    response_model=list[MessageOut],
    dependencies=[Depends(require_module("email"))],
)
def list_messages(
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    folder_id: str | None = Query(None, description="Graph folder id; None = all folders"),
) -> list[MessageOut]:
    """Folder-scoped or unified inbox. Returns messages visible to the user."""
    uid = _user_id(user)
    role = _user_role(user)
    q = tenant_db.query(OutlookMessage)
    if folder_id:
        q = q.filter(OutlookMessage.folder_id == folder_id)
    rows = (
        q.order_by(desc(OutlookMessage.received_at))
        .offset(offset)
        .limit(limit)
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    return [_to_out(m) for m in visible]


@router.get(
    "/messages/by-customer/{customer_id}",
    response_model=list[MessageOut],
    dependencies=[Depends(require_module("email"))],
)
def list_by_customer(
    customer_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> list[MessageOut]:
    """All messages tagged to this customer that the current user may see."""
    uid = _user_id(user)
    role = _user_role(user)
    rows = (
        tenant_db.query(OutlookMessage)
        .filter(OutlookMessage.linked_customer_id == customer_id)
        .order_by(desc(OutlookMessage.received_at))
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    return [_to_out(m) for m in visible]


@router.get(
    "/messages/by-job/{job_id}",
    response_model=list[MessageOut],
    dependencies=[Depends(require_module("email"))],
)
def list_by_job(
    job_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> list[MessageOut]:
    """All messages tagged to this job that the current user may see."""
    uid = _user_id(user)
    role = _user_role(user)
    rows = (
        tenant_db.query(OutlookMessage)
        .filter(OutlookMessage.linked_job_id == job_id)
        .order_by(desc(OutlookMessage.received_at))
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    return [_to_out(m) for m in visible]


@router.get(
    "/messages/{message_id}",
    response_model=MessageDetailOut,
    dependencies=[Depends(require_module("email"))],
)
def get_message_detail(
    message_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> MessageDetailOut:
    """Full message detail. 404 if not found OR not visible to viewer."""
    uid = _user_id(user)
    role = _user_role(user)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        # 404 (not 403) — never confirm existence to unauthorized callers.
        raise HTTPException(status_code=404, detail="message not found")
    return _to_detail(msg, viewer_is_owner=_viewer_owns_mailbox(tenant_db, msg, uid))


@router.post(
    "/messages/{message_id}/personal",
    response_model=MessageDetailOut,
    dependencies=[Depends(require_module("email"))],
)
def set_message_personal(
    message_id: UUID,
    payload: PersonalIn,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> MessageDetailOut:
    """Mark/unmark a message personal — OWNER ONLY.

    ``is_personal=True`` is the per-message privacy override: the ACL
    chokepoint (visibility.can_view) shows a personal message to nobody but
    the mailbox owner, regardless of every tenant rule. Only the owner may
    flip it — matching the existing write-action posture (mark-read/move are
    owner-only too), and because letting an admin mark someone ELSE's mail
    personal would hide it from every other admin.
    """
    uid = _user_id(user)
    role = _user_role(user)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        # 404 (not 403) — never confirm existence to unauthorized callers.
        raise HTTPException(status_code=404, detail="message not found")
    if not _viewer_owns_mailbox(tenant_db, msg, uid):
        raise HTTPException(
            status_code=403,
            detail="only the mailbox owner can mark a message personal",
        )
    msg.is_personal = payload.is_personal
    tenant_db.commit()
    return _to_detail(msg, viewer_is_owner=True)


def _tenant_id(user: dict[str, Any]) -> UUID:
    raw = user.get("tenant_id")
    if not raw:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


@router.get(
    "/messages/{message_id}/body",
    response_model=MessageBodyOut,
    dependencies=[Depends(require_module("email"))],
)
def get_message_body(
    message_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
    control_db: Session = Depends(get_db),
) -> MessageBodyOut:
    """Live-fetch the full HTML body for one message (D1).

    Rather than persist bodies (R2), we fetch on open: no migration, always
    fresh. Two load-bearing rules:

    * **Visibility first.** Same ``can_view`` chokepoint + 404 (never 403) as
      the detail endpoint — a viewer who can't see the message can't read its
      body.
    * **Owner token, not viewer token.** This is a SHARED mailbox: the viewer
      is frequently not the account owner, and ``with_outlook_client`` keys
      tokens off the passed user_id. We resolve the mailbox OWNER
      (``mailbox_owner_id`` → the account's user_id) and fetch as them, so a
      tech/second-office viewer gets the body instead of "reconnect".

    Never raises on a Graph problem — falls back to ``body_preview`` with
    ``fetched=False`` + a reason, so the pane degrades instead of erroring.
    """
    uid = _user_id(user)
    role = _user_role(user)
    tid = _tenant_id(user)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        raise HTTPException(status_code=404, detail="message not found")

    preview = msg.body_preview

    # A local draft never existed on Graph — nothing to fetch.
    graph_id = msg.graph_message_id or ""
    if not graph_id or graph_id.startswith("local-draft-"):
        return MessageBodyOut(
            fetched=False, body_preview=preview, reason="no_remote_copy"
        )

    owner_id = mailbox_owner_id(msg, tenant_db)
    if not owner_id:
        return MessageBodyOut(
            fetched=False, body_preview=preview, reason="no_account_owner"
        )

    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError  # noqa: PLC0415
    from gdx_dispatch.modules.outlook.token_refresh import (  # noqa: PLC0415
        OutlookReconnectRequired,
        OutlookTransientRetry,
        with_outlook_client,
    )

    owner_uid = UUID(str(owner_id))

    def _fetch_once() -> dict:
        with with_outlook_client(control_db, tenant_db, owner_uid, tid) as gc:
            return gc.get_message(graph_id)

    try:
        try:
            remote = _fetch_once()
        except OutlookTransientRetry:
            # Documented contract (token_refresh.py): a 401 mid-call after a
            # successful refresh deserves exactly one re-issue. Same as
            # transactional_email._send_once.
            remote = _fetch_once()
    except OutlookReconnectRequired:
        return MessageBodyOut(
            fetched=False, body_preview=preview, reason="reconnect_required"
        )
    except OutlookGraphAPIError as exc:
        # 404 = moved/deleted on Graph; anything else = transient upstream.
        reason = "message_gone" if getattr(exc, "status_code", None) == 404 else "graph_error"
        log.info("get_message_body: graph fetch failed id=%s reason=%s", message_id, reason)
        return MessageBodyOut(fetched=False, body_preview=preview, reason=reason)
    except Exception:  # noqa: BLE001 — never 500 the read pane on a body fetch
        log.exception("get_message_body: unexpected error id=%s", message_id)
        return MessageBodyOut(fetched=False, body_preview=preview, reason="graph_error")

    body = (remote or {}).get("body") or {}
    raw = body.get("content")
    ctype = (body.get("contentType") or "").lower()
    if not raw:
        return MessageBodyOut(fetched=False, body_preview=preview, reason="empty_body")
    return MessageBodyOut(
        fetched=True,
        content_type="text" if ctype == "text" else "html",
        body_html=raw,
        body_preview=preview,
    )
