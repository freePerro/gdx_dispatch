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

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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


class LinkIn(BaseModel):
    customer_id: UUID | None = None
    job_id: UUID | None = None


# Roles allowed to (re)assign a message's customer/job link. Office staff, not
# field techs — matching the tagged-visibility posture (techs consume tags,
# they don't curate them).
_TAG_MANAGER_ROLES = {"owner", "admin", "dispatcher", "csr", "manager", "sales"}


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


class AttachmentItem(BaseModel):
    id: str
    name: str | None = None
    content_type: str | None = None
    size: int | None = None
    is_inline: bool = False


class AttachmentsOut(BaseModel):
    """Lazy attachment listing for one message (D4). ``fetched`` is False when
    the owner-token Graph call couldn't run — caller shows ``reason``."""
    fetched: bool
    attachments: list[AttachmentItem] = []
    reason: str | None = None


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


# Worst-case windows one /messages request will scan skipping fully-hidden
# pages. limit≤200, so ≤ 200*_MAX_PAGE_SCANS rows examined per request.
_MAX_PAGE_SCANS = 8


class MessageListOut(BaseModel):
    """Paginated inbox page (D7).

    ``offset`` paginates the RAW rows BEFORE the Python visibility filter, so
    every message is reachable by paging even though a given page may return
    fewer than ``per_page`` visible items (some are filtered out). Hence
    ``has_more`` is derived from the raw window being full, and per-page
    ``len(items)`` is approximate under the visibility filter.
    """
    items: list[MessageOut]
    has_more: bool
    next_offset: int


@router.get(
    "/messages",
    response_model=MessageListOut,
    dependencies=[Depends(require_module("email"))],
)
def list_messages(
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    folder_id: str | None = Query(None, description="Graph folder id; None = all folders"),
) -> MessageListOut:
    """Folder-scoped or unified inbox, paginated.

    D7: the old version applied ``.offset().limit()`` in SQL and THEN
    ``filter_visible`` in Python, so a page could silently drop rows and there
    was no way to reach page 2 — mail fell off the bottom. Now ``offset``/
    ``limit`` page the raw rows and the response carries ``has_more`` +
    ``next_offset`` so the client can load every message.
    """
    uid = _user_id(user)
    role = _user_role(user)
    q = tenant_db.query(OutlookMessage)
    if folder_id:
        q = q.filter(OutlookMessage.folder_id == folder_id)
    # id is a tiebreaker so equal received_at rows have a STABLE order across
    # pages (else offset pagination can skip/duplicate them). nulls_last:
    # Postgres sorts NULLs FIRST on DESC — a row missing received_at (e.g. a
    # partial-sync remnant) would otherwise pin itself above all real mail.
    q = q.order_by(desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id))
    tech_emails = _load_tech_emails(tenant_db)

    # Skip windows the visibility filter empties, SERVER-SIDE, so a restricted
    # viewer never gets a run of empty "Load more" pages (a tech seeing 30 of
    # 5000 rows would otherwise click through ~100 blank pages). Bounded by
    # _MAX_PAGE_SCANS so one request can't walk the whole mailbox.
    cur = offset
    visible: list[OutlookMessage] = []
    reached_end = False
    for _ in range(_MAX_PAGE_SCANS):
        rows = q.offset(cur).limit(limit).all()
        cur += len(rows)
        if len(rows) < limit:
            reached_end = True
        visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
        if visible or reached_end:
            break
        # whole window hidden but more rows remain → advance to the next window
    return MessageListOut(
        items=[_to_out(m) for m in visible],
        has_more=not reached_end,
        next_offset=cur,
    )


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
        .order_by(desc(OutlookMessage.received_at).nulls_last())
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
        .order_by(desc(OutlookMessage.received_at).nulls_last())
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


@router.post(
    "/messages/{message_id}/link",
    response_model=MessageDetailOut,
    dependencies=[Depends(require_module("email"))],
)
def link_message(
    message_id: UUID,
    payload: LinkIn,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> MessageDetailOut:
    """Manually link a message to a customer and/or job (D3).

    Sets the tag to ``manual`` (confidence 1.0), overriding any auto-tag —
    the correction path when auto_match/job_thread guessed wrong or missed.
    Office roles only; the viewer must also be able to see the message.
    """
    uid = _user_id(user)
    role = _user_role(user)
    if payload.customer_id is None and payload.job_id is None:
        raise HTTPException(status_code=422, detail="provide customer_id and/or job_id")
    if role not in _TAG_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="not permitted to link messages")
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        raise HTTPException(status_code=404, detail="message not found")

    # Validate the targets exist and aren't soft-deleted — otherwise a typo'd
    # id 500s on insert and a deleted customer links silently (auto_match
    # excludes deleted_at; the manual path must too).
    from gdx_dispatch.models.tenant_models import Customer, Job  # noqa: PLC0415

    if payload.customer_id is not None:
        cust = (
            tenant_db.query(Customer.id)
            .filter(Customer.id == payload.customer_id, Customer.deleted_at.is_(None))
            .first()
        )
        if cust is None:
            raise HTTPException(status_code=422, detail="customer_id not found")
    if payload.job_id is not None:
        job = (
            tenant_db.query(Job.id)
            .filter(Job.id == payload.job_id, Job.deleted_at.is_(None))
            .first()
        )
        if job is None:
            raise HTTPException(status_code=422, detail="job_id not found")

    from gdx_dispatch.modules.outlook.tagger import manual_tag  # noqa: PLC0415

    manual_tag(msg, customer_id=payload.customer_id, job_id=payload.job_id)
    tenant_db.commit()
    return _to_detail(msg, viewer_is_owner=_viewer_owns_mailbox(tenant_db, msg, uid))


@router.delete(
    "/messages/{message_id}/link",
    response_model=MessageDetailOut,
    dependencies=[Depends(require_module("email"))],
)
def unlink_message(
    message_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> MessageDetailOut:
    """Clear a message's customer/job link (D3).

    Records a MANUAL 'no link' (tag_strategy='manual', links NULL) rather than
    resetting to NULL — otherwise the hourly retag would just re-apply the very
    auto-tag the user is rejecting. The human decision is durable; re-link with
    POST /link to change it. Office roles + can_view, same as link.
    """
    uid = _user_id(user)
    role = _user_role(user)
    if role not in _TAG_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="not permitted to unlink messages")
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        raise HTTPException(status_code=404, detail="message not found")
    from gdx_dispatch.modules.outlook.tagger import manual_tag  # noqa: PLC0415

    # manual_tag with no ids: links NULL, strategy 'manual' — pins it so
    # neither tag_message (skips tagged) nor the retag (WHERE tag_strategy IS
    # NULL) re-links it.
    manual_tag(msg)
    tenant_db.commit()
    return _to_detail(msg, viewer_is_owner=_viewer_owns_mailbox(tenant_db, msg, uid))


def _tenant_id(user: dict[str, Any]) -> UUID:
    raw = user.get("tenant_id")
    if not raw:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


class _OwnerFetchError(Exception):
    """A live-fetch against the mailbox owner's Graph token could not complete.

    ``reason`` is a stable machine code the caller maps to UX:
    no_remote_copy | no_account_owner | reconnect_required | message_gone |
    graph_error.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _owner_graph(msg, control_db, tenant_db, tid, op):
    """Run ``op(gc, graph_message_id)`` against the MAILBOX OWNER's Graph token.

    The single owner-token path shared by the body (D1) and attachment (D4)
    endpoints so they can't drift. Shared mailbox → resolve the owner
    (``mailbox_owner_id``) and use THEIR token, never the viewer's; honor the
    retry-once ``OutlookTransientRetry`` contract; translate every Graph
    failure into ``_OwnerFetchError(reason)`` instead of a 500.
    """
    graph_id = getattr(msg, "graph_message_id", None) or ""
    if not graph_id or graph_id.startswith("local-draft-"):
        raise _OwnerFetchError("no_remote_copy")
    owner_id = mailbox_owner_id(msg, tenant_db)
    if not owner_id:
        raise _OwnerFetchError("no_account_owner")

    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError  # noqa: PLC0415
    from gdx_dispatch.modules.outlook.token_refresh import (  # noqa: PLC0415
        OutlookReconnectRequired,
        OutlookTransientRetry,
        with_outlook_client,
    )

    owner_uid = UUID(str(owner_id))

    def _once():
        with with_outlook_client(control_db, tenant_db, owner_uid, tid) as gc:
            return op(gc, graph_id)

    try:
        try:
            return _once()
        except OutlookTransientRetry:
            return _once()
    except OutlookReconnectRequired:
        raise _OwnerFetchError("reconnect_required") from None
    except OutlookGraphAPIError as exc:
        reason = "message_gone" if getattr(exc, "status_code", None) == 404 else "graph_error"
        log.info("owner graph fetch failed reason=%s", reason)
        raise _OwnerFetchError(reason) from None
    except _OwnerFetchError:
        raise
    except Exception:  # noqa: BLE001 — never 500 a read pane on a live fetch
        log.exception("owner graph fetch: unexpected error")
        raise _OwnerFetchError("graph_error") from None


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

    try:
        remote = _owner_graph(
            msg, control_db, tenant_db, tid, lambda gc, gid: gc.get_message(gid)
        )
    except _OwnerFetchError as exc:
        return MessageBodyOut(fetched=False, body_preview=preview, reason=exc.reason)

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


# Largest attachment we'll stream through the app. download_attachment buffers
# the whole blob in memory, so refuse oversized files rather than OOM the
# worker; the pane still lists them (with size) so the user isn't surprised.
_MAX_ATTACHMENT_BYTES = 35 * 1024 * 1024


def _attachments_of(msg, control_db, tenant_db, tid) -> list[dict]:
    """Owner-token list of a message's attachments (raw Graph dicts)."""
    raw = _owner_graph(
        msg, control_db, tenant_db, tid, lambda gc, gid: gc.list_attachments(gid)
    )
    return raw or []


def _is_file_attachment(a: dict) -> bool:
    """Only fileAttachments have downloadable bytes at /$value. item- and
    reference-attachments (an email-as-attachment, a OneDrive link) would 502
    on download, so keep them out of the tray. Absent discriminator → assume
    file (Graph omits it only for a homogeneous fileAttachment collection)."""
    otype = a.get("@odata.type") or ""
    return not otype or "fileattachment" in otype.lower()


@router.get(
    "/messages/{message_id}/attachments",
    response_model=AttachmentsOut,
    dependencies=[Depends(require_module("email"))],
)
def list_message_attachments(
    message_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
    control_db: Session = Depends(get_db),
) -> AttachmentsOut:
    """List a message's attachments (D4), lazily on open.

    Not fetched during bulk sync (that would fire an extra Graph call per
    message every poll). Owner-token + can_view gated, same as the body.
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

    try:
        raw = _attachments_of(msg, control_db, tenant_db, tid)
    except _OwnerFetchError as exc:
        return AttachmentsOut(fetched=False, reason=exc.reason)

    items = [
        AttachmentItem(
            id=str(a.get("id") or ""),
            name=a.get("name"),
            content_type=a.get("contentType"),
            size=a.get("size"),
            is_inline=bool(a.get("isInline")),
        )
        for a in raw
        if a.get("id") and _is_file_attachment(a)
    ]
    return AttachmentsOut(fetched=True, attachments=items)


def _safe_filename(name: str | None) -> str:
    """Strip CR/LF/quotes so a crafted attachment name can't inject a header,
    and fall back to a generic name when empty. May still contain non-ASCII —
    _content_disposition handles that."""
    cleaned = "".join(c for c in (name or "") if c not in '\r\n"\\' and ord(c) >= 32)
    cleaned = cleaned.strip()
    return cleaned or "attachment"


def _content_disposition(name: str | None) -> str:
    """Build a Content-Disposition safe for BOTH the header codec and browsers.

    Starlette encodes header values as latin-1, so a raw CJK/emoji/accented
    filename in filename="…" 500s (UnicodeEncodeError). RFC 5987 fixes it: an
    ASCII-only `filename=` fallback for old clients plus a UTF-8 percent-encoded
    `filename*=` that modern browsers prefer.
    """
    import urllib.parse  # noqa: PLC0415

    safe = _safe_filename(name)
    ascii_fallback = safe.encode("ascii", "ignore").decode("ascii").strip() or "attachment"
    utf8 = urllib.parse.quote(safe, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8}"


@router.get(
    "/messages/{message_id}/attachments/{attachment_id}",
    dependencies=[Depends(require_module("email"))],
)
def download_message_attachment(
    message_id: UUID,
    attachment_id: str,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
    control_db: Session = Depends(get_db),
):
    """Download one attachment (D4). Owner-token + can_view gated.

    Looks the attachment up in the message's listing first (to get its name,
    content-type, and declared size) so we can refuse oversized files BEFORE
    pulling the bytes, then streams the blob back as an attachment download.
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

    try:
        listing = _attachments_of(msg, control_db, tenant_db, tid)
    except _OwnerFetchError as exc:
        # message_gone → 404; anything else (reconnect / graph) → 502 upstream.
        code = 404 if exc.reason in ("message_gone", "no_remote_copy") else 502
        raise HTTPException(status_code=code, detail=f"attachment unavailable: {exc.reason}") from None

    att = next((a for a in listing if str(a.get("id")) == attachment_id), None)
    if att is None:
        raise HTTPException(status_code=404, detail="attachment not found")

    size = att.get("size")
    if isinstance(size, int) and size > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large to download here")

    try:
        data = _owner_graph(
            msg, control_db, tenant_db, tid,
            lambda gc, gid: gc.download_attachment(gid, attachment_id),
        )
    except _OwnerFetchError as exc:
        code = 404 if exc.reason in ("message_gone", "no_remote_copy") else 502
        raise HTTPException(status_code=code, detail=f"attachment unavailable: {exc.reason}") from None

    if not isinstance(data, (bytes, bytearray)):
        raise HTTPException(status_code=502, detail="attachment unavailable: bad_response")
    if len(data) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large to download here")

    media_type = att.get("contentType") or "application/octet-stream"
    # Buffered Response, not StreamingResponse: download_attachment already
    # pulled the whole blob into memory, so a single-chunk "stream" would be
    # theater — a plain Response is honest and sets Content-Length.
    return Response(
        content=bytes(data),
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition(att.get("name"))},
    )
