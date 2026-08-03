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
from pydantic import BaseModel, Field
from sqlalchemy import desc, or_
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
    # P2.1 — human labels for the link badge. A UUID on a mail row tells the
    # reader nothing; "Acme Doors / JOB-2026-014" is the entire point of
    # reading mail inside GDX. Resolved in ONE batched query per page (see
    # _link_labels), never per row.
    linked_customer_name: str | None = None
    linked_job_label: str | None = None
    conversation_id: str | None = None
    tag_strategy: str | None = None
    is_personal: bool


class MessageDetailOut(MessageOut):
    cc_addresses: list[str] | None = None
    bcc_addresses: list[str] | None = None
    # conversation_id now lives on MessageOut (the list needs it to group a
    # thread) — do NOT redeclare it here, or _to_detail's **base spread
    # collides with the explicit kwarg.
    internet_message_id: str | None = None
    body_r2_key: str | None = None
    # The address of the mailbox this message was synced FROM (the connected
    # account's UPN). Reply-all needs it: on a shared office inbox the mailbox's
    # own address is in the original To/Cc, so without it every reply-all CCs
    # the inbox back into itself and the thread doubles each round-trip.
    mailbox_address: str | None = None
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


def _link_labels(
    tenant_db: Session, rows: list[OutlookMessage],
) -> tuple[dict[str, str], dict[str, str]]:
    """Batch-resolve (customer_id → name) and (job_id → label) for a page.

    Two queries for the whole page, never one per row. Best-effort: a lookup
    failure costs the badge its label, never the mail list — so it degrades to
    "linked, unnamed" instead of 500ing the inbox.
    """
    cust_ids = {m.linked_customer_id for m in rows if m.linked_customer_id}
    job_ids = {m.linked_job_id for m in rows if m.linked_job_id}
    customers: dict[str, str] = {}
    jobs: dict[str, str] = {}
    if not cust_ids and not job_ids:
        return customers, jobs
    try:
        from gdx_dispatch.models.tenant_models import Customer, Job  # noqa: PLC0415

        if cust_ids:
            for cid, name in (
                tenant_db.query(Customer.id, Customer.name)
                .filter(Customer.id.in_(cust_ids))
                .all()
            ):
                if name:
                    customers[str(cid)] = name
        if job_ids:
            for jid, number, title in (
                tenant_db.query(Job.id, Job.job_number, Job.title)
                .filter(Job.id.in_(job_ids))
                .all()
            ):
                # job_number is NULL on legacy rows — fall back to the title,
                # then to a short id, so the badge always says something.
                jobs[str(jid)] = number or title or f"Job {str(jid)[:8]}"
    except Exception:  # noqa: BLE001
        log.warning("views_router: link-label lookup failed — badges render unlabeled", exc_info=True)
    return customers, jobs


def _to_out(
    m: OutlookMessage,
    *,
    customers: dict[str, str] | None = None,
    jobs: dict[str, str] | None = None,
) -> MessageOut:
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
        linked_customer_name=(customers or {}).get(str(m.linked_customer_id)),
        linked_job_label=(jobs or {}).get(str(m.linked_job_id)),
        conversation_id=m.conversation_id,
        tag_strategy=m.tag_strategy,
        is_personal=m.is_personal,
    )


def _to_out_all(tenant_db: Session, rows: list[OutlookMessage]) -> list[MessageOut]:
    """Serialize a page WITH link labels (one batched lookup for the page)."""
    customers, jobs = _link_labels(tenant_db, rows)
    return [_to_out(m, customers=customers, jobs=jobs) for m in rows]


def _mailbox_address(tenant_db: Session, msg: OutlookMessage) -> str | None:
    """The connected account's own address (UPN) for this message's mailbox.

    Best-effort — a missing address only costs reply-all its self-drop.
    """
    try:
        from gdx_dispatch.modules.outlook.models import OutlookAccount  # noqa: PLC0415

        row = (
            tenant_db.query(OutlookAccount.upn)
            .filter(OutlookAccount.id == msg.account_id)
            .first()
        )
        value = row[0] if row else None
        return value.strip().lower() if isinstance(value, str) and value.strip() else None
    except Exception:  # noqa: BLE001
        log.warning("views_router: mailbox-address lookup failed", exc_info=True)
        return None


def _to_detail(
    m: OutlookMessage,
    *,
    viewer_is_owner: bool = False,
    tenant_db: Session | None = None,
) -> MessageDetailOut:
    customers, jobs = _link_labels(tenant_db, [m]) if tenant_db is not None else ({}, {})
    base = _to_out(m, customers=customers, jobs=jobs).model_dump()
    return MessageDetailOut(
        **base,
        cc_addresses=m.cc_addresses,
        bcc_addresses=m.bcc_addresses,
        internet_message_id=m.internet_message_id,
        body_r2_key=m.body_r2_key,
        viewer_is_owner=viewer_is_owner,
        mailbox_address=_mailbox_address(tenant_db, m) if tenant_db is not None else None,
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

# Cap on the customer/job Email tab (P2.1) and the conversation view (1.3).
# Newest-first, so the cap trims the oldest tail rather than hiding recent mail.
_MAX_TIMELINE_ROWS = 200

# Rows the unread badge (P2.6) scans before it gives up counting exactly. The
# badge renders "99+" well before this, so an approximate tail is invisible.
_UNREAD_SCAN_LIMIT = 500


def _search_predicate(term: str):
    """Case-insensitive substring match over subject / sender / preview (1.1).

    LIKE wildcards in the user's text are ESCAPED — otherwise a search for
    "50%" matches every message ("%" is "any run of characters"), and "_"
    silently matches any single char. The escape char is declared to the DB so
    Postgres and SQLite agree.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    return or_(
        OutlookMessage.subject.ilike(like, escape="\\"),
        OutlookMessage.from_address.ilike(like, escape="\\"),
        OutlookMessage.body_preview.ilike(like, escape="\\"),
    )


# Raw rows a search scans before it stops. Search pages over the VISIBLE
# results inside this window (see _search_page), so the window has to be big
# enough that a normal search never truncates.
_SEARCH_SCAN_LIMIT = 500


def _search_page(
    tenant_db: Session,
    query,
    search: str,
    uid: UUID,
    role: str,
    *,
    limit: int,
    offset: int,
) -> MessageListOut:
    """Paginate a SEARCH over the rows the viewer can actually see.

    **This is a privacy boundary, not a convenience.** The unsearched list
    pages over RAW rows and reports a raw cursor — fine for "show me my mail",
    but with a caller-supplied substring that same cursor becomes a content
    oracle: ``?q=<guess>&limit=1`` would return ``items: []`` with a
    ``next_offset`` that counts HIDDEN matches, letting anyone confirm words
    inside mail they are forbidden to open (personal / owner_only). Probe by
    probe, that reconstructs content.

    So the search path filters FIRST and then paginates the visible list:
    every number in the response — ``items``, ``has_more``, ``next_offset`` —
    is derived only from rows this viewer may read. ``offset`` therefore
    indexes VISIBLE results here, not raw rows.

    The term predicate is applied HERE, not by the caller — keeping the two
    halves of "search" (match, then authorize) in one function is what stops a
    future edit from separating them again.
    """
    query = query.filter(_search_predicate(search))
    rows = (
        query.order_by(
            desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id)
        )
        .limit(_SEARCH_SCAN_LIMIT)
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    page = visible[offset : offset + limit]
    return MessageListOut(
        items=_to_out_all(tenant_db, page),
        has_more=len(visible) > offset + limit,
        next_offset=offset + len(page),
    )


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
    q: str | None = Query(
        None,
        max_length=200,
        description="Search subject / sender / preview (case-insensitive substring)",
    ),
) -> MessageListOut:
    """Folder-scoped or unified inbox, paginated, optionally searched (1.1).

    D7: the old version applied ``.offset().limit()`` in SQL and THEN
    ``filter_visible`` in Python, so a page could silently drop rows and there
    was no way to reach page 2 — mail fell off the bottom. Now ``offset``/
    ``limit`` page the raw rows and the response carries ``has_more`` +
    ``next_offset`` so the client can load every message.

    ``q`` searches the LOCALLY STORED columns only — subject, sender, and the
    ~255-char preview. It does NOT search message bodies: bodies are never
    persisted (D1 live-fetches them), so a local body search would silently
    return nothing for text that is plainly in the email. Graph ``$search``
    is the follow-up if preview-scope proves too thin.
    """
    uid = _user_id(user)
    role = _user_role(user)
    search = (q or "").strip()
    query = tenant_db.query(OutlookMessage)
    if folder_id:
        query = query.filter(OutlookMessage.folder_id == folder_id)
    if search:
        return _search_page(tenant_db, query, search, uid, role, limit=limit, offset=offset)
    # id is a tiebreaker so equal received_at rows have a STABLE order across
    # pages (else offset pagination can skip/duplicate them). nulls_last:
    # Postgres sorts NULLs FIRST on DESC — a row missing received_at (e.g. a
    # partial-sync remnant) would otherwise pin itself above all real mail.
    query = query.order_by(
        desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id)
    )
    tech_emails = _load_tech_emails(tenant_db)

    # Skip windows the visibility filter empties, SERVER-SIDE, so a restricted
    # viewer never gets a run of empty "Load more" pages (a tech seeing 30 of
    # 5000 rows would otherwise click through ~100 blank pages). Bounded by
    # _MAX_PAGE_SCANS so one request can't walk the whole mailbox.
    cur = offset
    visible: list[OutlookMessage] = []
    reached_end = False
    for _ in range(_MAX_PAGE_SCANS):
        rows = query.offset(cur).limit(limit).all()
        cur += len(rows)
        if len(rows) < limit:
            reached_end = True
        visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
        if visible or reached_end:
            break
        # whole window hidden but more rows remain → advance to the next window
    return MessageListOut(
        items=_to_out_all(tenant_db, visible),
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
    """All messages tagged to this customer that the current user may see.

    Capped at ``_MAX_TIMELINE_ROWS`` newest-first: this feeds the customer
    Email tab (P2.1), and a customer with years of correspondence would
    otherwise serialize the whole history into one response.
    """
    uid = _user_id(user)
    role = _user_role(user)
    rows = (
        tenant_db.query(OutlookMessage)
        .filter(OutlookMessage.linked_customer_id == customer_id)
        .order_by(desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id))
        .limit(_MAX_TIMELINE_ROWS)
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    return _to_out_all(tenant_db, visible)


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
    """All messages tagged to this job that the current user may see (capped)."""
    uid = _user_id(user)
    role = _user_role(user)
    rows = (
        tenant_db.query(OutlookMessage)
        .filter(OutlookMessage.linked_job_id == job_id)
        .order_by(desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id))
        .limit(_MAX_TIMELINE_ROWS)
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    return _to_out_all(tenant_db, visible)


class UnreadCountOut(BaseModel):
    """Unread count for the nav badge (P2.6).

    Deliberately just a count: an earlier shape also returned whether the scan
    window was full, which told a viewer who can see NOTHING that the mailbox
    holds at least 500 unread messages. The scan cap is an implementation
    detail, so it stays in the log.
    """
    count: int


# Folders whose unread mail must NOT badge the nav. The badge's click target
# is the Inbox; counting Junk and Deleted Items there means a badge of 23 that
# opens onto 4 unread messages, and a "New email" toast every time spam lands.
_UNBADGED_FOLDERS = ("junkemail", "deleteditems", "drafts", "sentitems", "outbox")


def _unbadged_folder_ids(tenant_db: Session) -> list[str]:
    """Graph folder ids for `_UNBADGED_FOLDERS`, or [] if unresolvable.

    Best-effort: if the folder cache can't be read we count everything rather
    than count nothing — an inflated badge is a nuisance, a silently zero
    badge hides real mail.
    """
    try:
        from gdx_dispatch.modules.outlook.models import OutlookFolder  # noqa: PLC0415

        rows = (
            tenant_db.query(OutlookFolder.graph_folder_id)
            .filter(OutlookFolder.well_known_name.in_(_UNBADGED_FOLDERS))
            .all()
        )
        return [r[0] for r in rows if r and isinstance(r[0], str)]
    except Exception:  # noqa: BLE001
        log.warning("unread_message_count: folder-exclusion lookup failed", exc_info=True)
        return []


# NOTE ordering: this route MUST stay above ``/messages/{message_id}``. That
# path param is typed UUID, so "unread-count" would 422 there rather than fall
# through to a later match.
@router.get(
    "/messages/unread-count",
    response_model=UnreadCountOut,
    dependencies=[Depends(require_module("email"))],
)
def unread_message_count(
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> UnreadCountOut:
    """Count unread messages THIS VIEWER may see — drives the sidebar badge.

    Two rules make the number mean what the badge implies:

    * **Visibility.** An exact SQL ``COUNT`` would lie — it would count mail
      the viewer can't open. We scan the newest ``_UNREAD_SCAN_LIMIT`` unread
      rows and filter those; the badge renders 99+ long before the cap bites.
    * **Folder scope.** Junk, Deleted Items, Drafts and Sent are excluded.
      The badge's click target is the Inbox, so counting spam there produces a
      badge that doesn't match the screen it opens.
    """
    uid = _user_id(user)
    role = _user_role(user)
    q = tenant_db.query(OutlookMessage).filter(OutlookMessage.is_read.is_(False))
    excluded = _unbadged_folder_ids(tenant_db)
    if excluded:
        q = q.filter(
            or_(
                OutlookMessage.folder_id.is_(None),
                OutlookMessage.folder_id.notin_(excluded),
            )
        )
    rows = (
        q.order_by(desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id))
        .limit(_UNREAD_SCAN_LIMIT)
        .all()
    )
    tech_emails = _load_tech_emails(tenant_db)
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    if len(rows) >= _UNREAD_SCAN_LIMIT:
        log.info("unread_message_count: scan window full (>=%d unread rows)", _UNREAD_SCAN_LIMIT)
    return UnreadCountOut(count=len(visible))


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
    return _to_detail(
        msg, viewer_is_owner=_viewer_owns_mailbox(tenant_db, msg, uid), tenant_db=tenant_db
    )


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
    return _to_detail(msg, viewer_is_owner=True, tenant_db=tenant_db)


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
    return _to_detail(
        msg, viewer_is_owner=_viewer_owns_mailbox(tenant_db, msg, uid), tenant_db=tenant_db
    )


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
    return _to_detail(
        msg, viewer_is_owner=_viewer_owns_mailbox(tenant_db, msg, uid), tenant_db=tenant_db
    )


@router.get(
    "/messages/{message_id}/thread",
    response_model=list[MessageOut],
    dependencies=[Depends(require_module("email"))],
)
def get_message_thread(
    message_id: UUID,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> list[MessageOut]:
    """The whole conversation this message belongs to (1.3), oldest first.

    ``conversation_id`` is already stored + indexed by the sync, so threading
    needs no schema change. Every sibling still runs through ``filter_visible``
    — being able to see one message in a thread does not grant the rest (a
    personal reply inside a shared thread stays hidden).

    A message with no ``conversation_id`` (or the only one in its thread)
    returns just itself, so the caller can render the strip unconditionally.
    """
    uid = _user_id(user)
    role = _user_role(user)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        raise HTTPException(status_code=404, detail="message not found")
    if not msg.conversation_id:
        return _to_out_all(tenant_db, [msg])
    # NEWEST-first in SQL, reversed for display. Ordering ascending and then
    # capping would keep the OLDEST 200 of a long thread and silently drop
    # every recent reply — the opposite of what someone reading a conversation
    # needs.
    rows = (
        tenant_db.query(OutlookMessage)
        .filter(OutlookMessage.conversation_id == msg.conversation_id)
        .order_by(desc(OutlookMessage.received_at).nulls_last(), desc(OutlookMessage.id))
        .limit(_MAX_TIMELINE_ROWS)
        .all()
    )
    visible = filter_visible(rows, uid, role, tenant_db, tech_emails=tech_emails)
    # The anchor is visible by definition (can_view passed above); if the
    # window missed it (a thread longer than the cap), put it back rather than
    # return a "conversation" without the message being read.
    if not any(getattr(m, "id", None) == msg.id for m in visible):
        visible.append(msg)
    # Chronological for the reader — sort here rather than trust SQL order,
    # since the re-added anchor would otherwise land at the end.
    visible.sort(key=lambda m: (m.received_at is None, m.received_at, str(m.id)))
    return _to_out_all(tenant_db, visible)


class TaskFromEmailIn(BaseModel):
    """Optional overrides for P2.2 'turn this email into a follow-up'."""
    title: str | None = Field(default=None, max_length=300)
    assigned_to: str | None = Field(default=None, max_length=36)
    priority: str = Field(default="low", pattern="^(low|medium|high|urgent)$")


class TaskFromEmailOut(BaseModel):
    id: str
    title: str


@router.post(
    "/messages/{message_id}/create-task",
    response_model=TaskFromEmailOut,
    status_code=201,
    dependencies=[Depends(require_module("email"))],
)
def create_task_from_message(
    message_id: UUID,
    payload: TaskFromEmailIn,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
) -> TaskFromEmailOut:
    """Turn a message into a PlannerTask follow-up (P2.2).

    Deliberately reuses the SAME ``PlannerTask`` surface the phone-capture
    feature feeds — an emailed request becomes a needs-action row in the
    morning digest with zero new reminder plumbing. ``due_date`` is stamped
    now for the same reason quick-capture does: the needs_action sort puts
    undated tasks last, so an undated capture would scroll away.

    Any viewer who can SEE the message can act on it; the task carries the
    message's customer/job links so it lands on the right record.
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

    # A PlannerTask is readable tenant-wide (routers/planner.py list_tasks with
    # view=all applies no per-user filter). Copying a message the owner
    # explicitly marked "🔒 Personal — visible only to you" into one would
    # publish its subject and preview to every technician — a one-way leak the
    # owner can't see happening. Refuse, and say why, instead of doing it
    # quietly.
    if msg.is_personal:
        raise HTTPException(
            status_code=409,
            detail=(
                "This message is marked personal. Tasks are visible to the whole "
                "team — make it shared first if you want a follow-up task."
            ),
        )

    from datetime import datetime, timezone  # noqa: PLC0415
    from uuid import uuid4  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import PlannerTask, User  # noqa: PLC0415
    from gdx_dispatch.routers.planner import calendar_today_utc  # noqa: PLC0415

    # An assignee that doesn't exist produces a task nobody will ever see in
    # their "mine" view — invisible work, the worst kind.
    if payload.assigned_to:
        assignee = (
            tenant_db.query(User.id)
            .filter(User.id == str(payload.assigned_to), User.deleted_at.is_(None))
            .first()
        )
        if assignee is None:
            raise HTTPException(status_code=422, detail="assigned_to is not a user in this tenant")

    subject = (msg.subject or "(no subject)").strip()
    title = (payload.title or f"Email: {subject}")[:300]
    body = (msg.body_preview or "").strip()
    description = f"From {msg.from_address or 'unknown sender'}\n\n{body}".strip()
    task = PlannerTask(
        id=str(uuid4()),
        company_id=str(tid),
        # \x00 breaks Postgres text columns — planner's create_task strips it
        # on the typed path; an email subject is untrusted input, so strip here
        # too rather than trust the mail server.
        title=title.replace("\x00", ""),
        description=description.replace("\x00", "")[:4000],
        status="todo",
        priority=payload.priority,
        # Business-local TODAY at the D@00:00-UTC convention — a raw now()
        # captured after ~7pm CDT carries tomorrow's UTC calendar day and
        # rendered "due tomorrow" (planner date fix, 2026-08-03).
        due_date=calendar_today_utc(),
        created_by=str(uid),
        assigned_to=payload.assigned_to or None,
        job_id=str(msg.linked_job_id) if msg.linked_job_id else None,
        customer_id=str(msg.linked_customer_id) if msg.linked_customer_id else None,
        source="email_capture",
        created_at=datetime.now(timezone.utc),
    )
    tenant_db.add(task)
    tenant_db.commit()
    log.info("create_task_from_message: task=%s message=%s", task.id, message_id)
    return TaskFromEmailOut(id=str(task.id), title=task.title)


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


class SaveAttachmentIn(BaseModel):
    """P2.3 — file an email attachment onto a job (and its customer)."""
    job_id: UUID
    description: str | None = Field(default=None, max_length=500)


class SaveAttachmentOut(BaseModel):
    document_id: str
    filename: str
    size_bytes: int
    already_saved: bool = False


def _document_upload_dir():
    """The same upload root the documents router writes to — imported, not
    re-derived, so a change to UPLOAD_DIR can't leave email-sourced files
    pointing at a directory the document reader never looks in."""
    from gdx_dispatch.routers.documents import _upload_dir  # noqa: PLC0415

    return _upload_dir()


@router.post(
    "/messages/{message_id}/attachments/{attachment_id}/save-to-job",
    response_model=SaveAttachmentOut,
    status_code=201,
    dependencies=[Depends(require_module("email"))],
)
def save_attachment_to_job(
    message_id: UUID,
    attachment_id: str,
    payload: SaveAttachmentIn,
    user: dict[str, Any] = Depends(get_user_for_views),
    tenant_db: Session = Depends(get_db_for_views),
    control_db: Session = Depends(get_db),
) -> SaveAttachmentOut:
    """Save one email attachment onto a job as a Document (P2.3).

    The PO, the signed contract, the spec sheet the supplier emailed — one
    click and it lives on the job record instead of only in a mailbox. Writes
    through the SAME storage path + Document row the documents module reads,
    so the file shows up in the job's Documents surface with no new plumbing.

    Idempotent by content hash: saving the same attachment to the same job
    twice returns the first Document instead of duplicating the bytes.
    """
    from pathlib import Path  # noqa: PLC0415
    from uuid import uuid4  # noqa: PLC0415

    uid = _user_id(user)
    role = _user_role(user)
    tid = _tenant_id(user)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    tech_emails = _load_tech_emails(tenant_db)
    if not can_view(msg, uid, role, tenant_db, tech_emails=tech_emails):
        raise HTTPException(status_code=404, detail="message not found")

    from gdx_dispatch.models.tenant_models import Document, Job  # noqa: PLC0415

    job = (
        tenant_db.query(Job.id, Job.customer_id)
        .filter(Job.id == payload.job_id, Job.deleted_at.is_(None))
        .first()
    )
    if job is None:
        raise HTTPException(status_code=422, detail="job_id not found")

    # Authorize the JOB, not just the message. Seeing an email must not confer
    # the right to write a file onto an arbitrary job: office roles may file to
    # any job (same posture as link_message), a tech only onto jobs that are
    # actually theirs (the job_belongs_to_user rule every other job-scoped
    # write uses). Without this, any viewer could attach a supplier's PDF to
    # any customer's job in the tenant.
    if role not in _TAG_MANAGER_ROLES:
        from gdx_dispatch.core.job_access import job_belongs_to_user  # noqa: PLC0415

        if not job_belongs_to_user(tenant_db, str(tid), str(payload.job_id), str(uid)):
            raise HTTPException(
                status_code=403,
                detail="not permitted to add files to this job",
            )

    try:
        listing = _attachments_of(msg, control_db, tenant_db, tid)
    except _OwnerFetchError as exc:
        code = 404 if exc.reason in ("message_gone", "no_remote_copy") else 502
        raise HTTPException(status_code=code, detail=f"attachment unavailable: {exc.reason}") from None
    att = next((a for a in listing if str(a.get("id")) == attachment_id), None)
    if att is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    size = att.get("size")
    if isinstance(size, int) and size > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large to save")

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
    data = bytes(data)
    if len(data) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large to save")

    # Truncate to the Document column widths BEFORE anything touches disk.
    # Attachment names come from whoever emailed us: a 300-char name passes
    # _safe_filename, the bytes get written, and then the INSERT raises
    # StringDataRightTruncation on Postgres — 500 to the user and an orphaned
    # blob in UPLOAD_DIR that every retry duplicates. (SQLite doesn't enforce
    # String(n), which is exactly why the mocked tests were happy.)
    original_name = _safe_filename(att.get("name"))[:255]
    content_type = (att.get("contentType") or "application/octet-stream")[:150]
    # Idempotency keyed on (job, name, size) — deliberately NOT on a content
    # hash. `Document.content_hash` is the vendor pipelines' tenant-wide dedup
    # key: vendor_statements.find_existing_document looks it up UNSCOPED and
    # hard-raises DuplicateDocumentError. Stamping a hash here would mean that
    # filing the supplier's emailed statement onto a job permanently blocks
    # importing that same statement as a vendor document.
    existing = (
        tenant_db.query(Document)
        .filter(
            Document.job_id == payload.job_id,
            Document.original_name == original_name,
            Document.file_size == len(data),
            Document.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return SaveAttachmentOut(
            document_id=str(existing.id),
            filename=existing.original_name,
            size_bytes=int(existing.file_size or 0),
            already_saved=True,
        )

    suffix = Path(original_name).suffix.lower()[:20]
    stored_filename = f"{uuid4()}{suffix}"
    upload_root = _document_upload_dir()
    stored_path = upload_root / stored_filename
    try:
        upload_root.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(data)
    except OSError:
        log.exception("save_attachment_to_job: write failed for %s", stored_filename)
        raise HTTPException(status_code=500, detail="could not store attachment") from None

    doc = Document(
        filename=stored_filename,
        original_name=original_name,
        file_size=len(data),
        content_type=content_type,
        uploaded_by=str(uid),
        title=original_name,
        description=(payload.description or f"From email: {(msg.subject or '').strip()}")[:500],
        job_id=payload.job_id,
        # customer_id is deliberately LEFT NULL. The customer portal lists
        # documents by customer_id (routers/portal.py portal_documents) and
        # returns original_name + title + description — so filing a supplier's
        # "Acme margin.pdf" with the subject "our markup on the Smith job"
        # would publish both straight to that customer's portal. The user
        # asked to put it on a JOB; job_id is what that means.
    )
    tenant_db.add(doc)
    try:
        tenant_db.commit()
    except Exception:
        # Never leave bytes on disk with no row pointing at them — a repeatable
        # failure would otherwise grow UPLOAD_DIR without bound.
        tenant_db.rollback()
        stored_path.unlink(missing_ok=True)
        log.exception("save_attachment_to_job: commit failed, rolled back and removed %s", stored_filename)
        raise HTTPException(status_code=500, detail="could not store attachment") from None
    log.info(
        "save_attachment_to_job: doc=%s job=%s message=%s bytes=%d",
        doc.id, payload.job_id, message_id, len(data),
    )
    return SaveAttachmentOut(
        document_id=str(doc.id),
        filename=original_name,
        size_bytes=len(data),
    )
