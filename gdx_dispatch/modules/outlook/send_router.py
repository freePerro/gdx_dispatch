"""Sprint Outlook Integration — Phase 6 send endpoint (+ P1 compose surface).

``POST /api/outlook/send`` — sends an email AS the current user via
Microsoft Graph ``/me/sendMail``. The sent message comes back via the
existing webhook + delta sync pipeline (Phase 2), so no separate "sent
folder sync" path is needed.

Also here, because they are all "compose" verbs against the same Graph
client and the same auth posture:

- ``POST /api/outlook/messages/{id}/forward`` (1.4) — native Graph forward,
  which carries the original attachments without a bytes round-trip.
- ``POST /api/outlook/drafts`` (1.5) — a REAL Graph draft, so it lands in the
  Drafts folder the rail already renders.
- ``POST /api/outlook/messages/{id}/ai-draft`` (P2.4) — a suggested reply body.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.models import OutlookMessage
from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired, with_outlook_client
from gdx_dispatch.modules.outlook.visibility import can_view, mailbox_owner_id
from gdx_dispatch.routers.auth import get_current_user


log = logging.getLogger("gdx_dispatch.modules.outlook.send_router")

router = APIRouter(
    prefix="/api/outlook",
    tags=["outlook", "send"],
)


class OutboundAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=120)
    # base64-encoded bytes; cap at 4MB raw → ~5.4MB encoded.
    content_base64: str = Field(min_length=1, max_length=8_000_000)


class SendMailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: list[EmailStr] = Field(min_length=1, max_length=50)
    cc: list[EmailStr] | None = None
    bcc: list[EmailStr] | None = None
    subject: str = Field(min_length=1, max_length=998)
    body_html: str = Field(min_length=1, max_length=1_000_000)
    in_reply_to: str | None = None             # OutlookMessage.id (UUID) of parent
    customer_id: UUID | None = None            # auto-tag the resulting sync row
    job_id: UUID | None = None
    save_to_sent_items: bool = True
    attachments: list[OutboundAttachment] | None = None


class SendMailOut(BaseModel):
    ok: bool
    detail: str | None = None


def get_user_for_send(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return user


def get_db_for_send(db: Session = Depends(get_db)) -> Session:
    return db


def _viewer_owns(msg: OutlookMessage, tenant_db: Session, uid: UUID) -> bool:
    """True when this viewer owns the mailbox the message was synced from.

    String comparison on purpose: ``OutlookAccount.user_id`` is String(36) and
    ``UUID('abc…') == 'abc…'`` is False in Python.
    """
    owner = mailbox_owner_id(msg, tenant_db)
    return owner is not None and str(owner) == str(uid)


def _job_number(tenant_db: Session, job_id: UUID | None) -> str | None:
    """The tenant's printed job number, for the customer-facing subject marker.

    Best-effort: a lookup failure just falls the marker back to the UUID form,
    which the tagger also understands — never blocks a send.
    """
    if job_id is None:
        return None
    try:
        from gdx_dispatch.models.tenant_models import Job  # noqa: PLC0415

        row = tenant_db.query(Job.job_number).filter(Job.id == job_id).first()
        value = row[0] if row else None
        # isinstance-checked, not just truthy: a legacy job has job_number
        # NULL, and this value goes straight into a customer-facing subject.
        return value.strip() if isinstance(value, str) and value.strip() else None
    except Exception:  # noqa: BLE001
        log.warning("send: job_number lookup failed for %s", job_id, exc_info=True)
        return None


def _tech_emails(tenant_db: Session) -> set[str]:
    """Known-tech mailbox addresses, for the visibility chokepoint's
    "tech recipient → all techs see it" rule.

    ``can_view`` silently treats a missing set as "no tech is a recipient",
    which made the send-side ACL disagree with the read-side one: a tech who
    can SEE a message in the inbox (views_router passes this set) got a 404
    from the send-side endpoints for the same message.
    """
    try:
        from gdx_dispatch.models.tenant_models import User  # noqa: PLC0415

        rows = (
            tenant_db.query(User.email)
            .filter(User.role.in_(["technician", "tech"]), User.deleted_at.is_(None))
            .all()
        )
        return {r[0].lower().strip() for r in rows if r and r[0]}
    except Exception:  # noqa: BLE001
        log.warning("send: tech-email preload failed — visibility rule degraded", exc_info=True)
        return set()


def _ids_from_user(user: dict[str, Any]) -> tuple[UUID, UUID]:
    user_id_raw = user.get("user_id") or user.get("id") or user.get("sub")
    tenant_id_raw = user.get("tenant_id")
    if not user_id_raw or not tenant_id_raw:
        raise HTTPException(status_code=400, detail="missing user/tenant context")
    uid = user_id_raw if isinstance(user_id_raw, UUID) else UUID(str(user_id_raw))
    tid = tenant_id_raw if isinstance(tenant_id_raw, UUID) else UUID(str(tenant_id_raw))
    return uid, tid


def job_marked_subject(subject: str, job_id: UUID | None, job_number: str | None = None) -> str:
    """Append the ``[Job #…]`` thread marker when sending about a job (P2.5).

    This closes the tagging loop: the tagger's ``job_thread`` strategy
    (tagger.py ``_JOB_PATTERNS``) reads exactly this marker off the SUBJECT,
    so the customer's reply — which quotes the subject — auto-links back to
    the job even when their reply comes from an address we've never seen.
    Without it, outbound job mail relies solely on address matching.

    The marker prefers the tenant's **job number** ("JOB-2026-014") over the
    UUID. This subject line is customer-facing: 36 characters of internal UUID
    in the subject of a quote reads as a system malfunction, and on a phone it
    pushes the real subject off the screen. The tagger resolves both forms.

    Idempotent: a subject that already carries the marker is left alone, so a
    long thread doesn't accumulate one per round-trip.
    """
    if job_id is None:
        return subject
    token = (job_number or "").strip() or str(job_id)
    marker = f"[Job #{token}]"
    existing = (subject or "").lower()
    # Look for the BRACKETED marker, not a bare substring of the token. A bare
    # scan means a job numbered "14" is treated as already-marked by any
    # subject containing "14" — "Quote for 14 doors" would silently never get
    # a marker (audit round 4). Both forms are checked because a thread may
    # already carry the older uuid marker.
    if marker.lower() in existing or f"[job #{job_id}]".lower() in existing:
        return subject
    # Graph rejects a subject over 998 chars (RFC 5322); keep room for the
    # marker by trimming the human part, never the marker.
    room = 998 - len(marker) - 1
    return f"{(subject or '')[:room]} {marker}".strip()


def _graph_attachments(payload: SendMailIn) -> list[dict] | None:
    if not payload.attachments:
        return None
    return [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": a.name,
            "contentType": a.content_type,
            "contentBytes": a.content_base64,
        }
        for a in payload.attachments
    ]


def _build_graph_body(payload: SendMailIn, job_number: str | None = None) -> dict:
    """Translate our SendMailIn into the Graph /me/sendMail wire format.

    Used for new conversations only. Replies go via /me/messages/{id}/reply
    so Graph wires In-Reply-To + References headers itself.
    """
    msg = {
        # Marker on NEW mail only — see send_mail for why replies never carry
        # one.
        "subject": (
            payload.subject
            if payload.in_reply_to
            else job_marked_subject(payload.subject, payload.job_id, job_number)
        ),
        "body": {"contentType": "html", "content": payload.body_html},
        "toRecipients": [{"emailAddress": {"address": str(a)}} for a in payload.to],
    }
    if payload.cc:
        msg["ccRecipients"] = [{"emailAddress": {"address": str(a)}} for a in payload.cc]
    if payload.bcc:
        msg["bccRecipients"] = [{"emailAddress": {"address": str(a)}} for a in payload.bcc]
    atts = _graph_attachments(payload)
    if atts:
        msg["attachments"] = atts
    return {
        "message": msg,
        "saveToSentItems": payload.save_to_sent_items,
    }


def _build_reply_body(payload: SendMailIn) -> dict:
    """Body for POST /me/messages/{id}/reply — Graph adds threading headers.

    Graph derives the reply subject from the parent ("Re: …"), so the job
    marker can't be stamped here; on the reply path the marker already rides
    along in the quoted subject when the outbound original carried one.
    """
    msg: dict[str, Any] = {
        "body": {"contentType": "html", "content": payload.body_html},
        "toRecipients": [{"emailAddress": {"address": str(a)}} for a in payload.to],
    }
    if payload.cc:
        msg["ccRecipients"] = [{"emailAddress": {"address": str(a)}} for a in payload.cc]
    if payload.bcc:
        msg["bccRecipients"] = [{"emailAddress": {"address": str(a)}} for a in payload.bcc]
    atts = _graph_attachments(payload)
    if atts:
        msg["attachments"] = atts
    return {"message": msg}


@router.post(
    "/send",
    response_model=SendMailOut,
    dependencies=[Depends(require_module("email"))],
)
def send_mail(
    payload: SendMailIn,
    user: dict[str, Any] = Depends(get_user_for_send),
    control_db: Session = Depends(get_db_for_send),
    tenant_db: Session = Depends(get_db_for_send),
) -> SendMailOut:
    """POST /me/sendMail via Graph as the current user."""
    user_id_raw = user.get("user_id") or user.get("id") or user.get("sub")
    tenant_id_raw = user.get("tenant_id")
    if not user_id_raw or not tenant_id_raw:
        raise HTTPException(status_code=400, detail="missing user/tenant context")
    uid = user_id_raw if isinstance(user_id_raw, UUID) else UUID(str(user_id_raw))
    tid = tenant_id_raw if isinstance(tenant_id_raw, UUID) else UUID(str(tenant_id_raw))

    parent_graph_id: str | None = None
    if payload.in_reply_to:
        try:
            parent_uuid = (
                payload.in_reply_to
                if isinstance(payload.in_reply_to, UUID)
                else UUID(str(payload.in_reply_to))
            )
        except (ValueError, AttributeError):
            parent_uuid = None
        if parent_uuid is not None:
            parent = (
                tenant_db.query(OutlookMessage)
                .filter(OutlookMessage.id == parent_uuid)
                .one_or_none()
            )
            # ACL on the parent: a viewer must be able to SEE a message to
            # thread a reply off it. Without this, replying to an arbitrary
            # UUID was an existence oracle for hidden (personal/owner_only)
            # mail — reply-vs-sendMail behavior differed. A hidden parent now
            # behaves exactly like a missing one (plain send, no threading).
            role = (user.get("role") or "viewer").lower()
            if (
                parent is not None
                and can_view(parent, uid, role, tenant_db, tech_emails=_tech_emails(tenant_db))
                and parent.graph_message_id
                # ...and the parent must live in THIS sender's mailbox.
                # /me/messages/{id}/reply resolves the id against the caller's
                # OWN mailbox: on a shared inbox, a non-owner replying with
                # the owner's graph id 404s at Graph and 502s the whole send.
                # Falling back to a plain sendMail loses RFC threading but
                # actually delivers the reply. (Plan: open decision 7 —
                # write actions stay owner-scoped.)
                and _viewer_owns(parent, tenant_db, uid)
            ):
                parent_graph_id = parent.graph_message_id

    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            if parent_graph_id:
                # Real RFC2822 threading: Graph wires In-Reply-To + References.
                gc._request(
                    "POST",
                    f"/me/messages/{parent_graph_id}/reply",
                    json=_build_reply_body(payload),
                )
            else:
                gc._request(
                    "POST", "/me/sendMail",
                    json=_build_graph_body(payload, _job_number(tenant_db, payload.job_id)),
                )
    except OutlookReconnectRequired as exc:
        log.warning("send_mail: reconnect required for %s: %s", uid, exc)
        raise HTTPException(
            status_code=409,
            detail="Outlook reconnect required — open Settings → Integrations → Outlook.",
        ) from exc
    except OutlookGraphAPIError as exc:
        log.warning("send_mail: graph error for %s: %s", uid, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Microsoft Graph rejected send: {exc.status_code}",
        ) from exc

    log.info("send_mail: ok user=%s recipients=%d", uid, len(payload.to))
    return SendMailOut(ok=True, detail=None)


# ── 1.4 forward ─────────────────────────────────────────────────────────


class ForwardMailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: list[EmailStr] = Field(min_length=1, max_length=50)
    cc: list[EmailStr] | None = None
    comment: str = Field(default="", max_length=100_000)


@router.post(
    "/messages/{message_id}/forward",
    response_model=SendMailOut,
    dependencies=[Depends(require_module("email"))],
)
def forward_message(
    message_id: UUID,
    payload: ForwardMailIn,
    user: dict[str, Any] = Depends(get_user_for_send),
    control_db: Session = Depends(get_db_for_send),
    tenant_db: Session = Depends(get_db_for_send),
) -> SendMailOut:
    """Forward a message via Graph's native ``/forward`` (1.4).

    Native forward — not a re-compose — because Graph carries the ORIGINAL
    ATTACHMENTS and quoted body itself. The plan's MVP was text-only forward
    with attachments as a follow-up; going through Graph gets attachments for
    free and avoids downloading + re-uploading every blob through our worker.

    **Owner-only.** ``/me/messages/{id}/forward`` resolves the id against the
    CALLER's mailbox, and forwarding from someone else's mailbox would also
    send mail under their name. Non-owners get a clear 403 instead of a
    confusing Graph 404. (Consistent with mark-read/move being owner-only —
    plan open decision 7.)
    """
    uid, tid = _ids_from_user(user)
    role = (user.get("role") or "viewer").lower()
    msg = tenant_db.query(OutlookMessage).filter(OutlookMessage.id == message_id).one_or_none()
    if msg is None or not can_view(msg, uid, role, tenant_db, tech_emails=_tech_emails(tenant_db)):
        # 404, never 403, for "can't see it" — same non-disclosure posture as
        # the read endpoints.
        raise HTTPException(status_code=404, detail="message not found")
    if not msg.graph_message_id or msg.graph_message_id.startswith("local-draft-"):
        raise HTTPException(status_code=409, detail="this message has no server copy to forward")
    if not _viewer_owns(msg, tenant_db, uid):
        raise HTTPException(
            status_code=403,
            detail="only the mailbox owner can forward this message",
        )

    body: dict[str, Any] = {
        "comment": payload.comment or "",
        "toRecipients": [{"emailAddress": {"address": str(a)}} for a in payload.to],
    }
    if payload.cc:
        # Graph's forward action has no ccRecipients field — the cc list rides
        # on the message object instead.
        body["message"] = {
            "ccRecipients": [{"emailAddress": {"address": str(a)}} for a in payload.cc]
        }
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            gc._request("POST", f"/me/messages/{msg.graph_message_id}/forward", json=body)
    except OutlookReconnectRequired as exc:
        raise HTTPException(
            status_code=409,
            detail="Outlook reconnect required — open Settings → Integrations → Outlook.",
        ) from exc
    except OutlookGraphAPIError as exc:
        log.warning("forward_message: graph error for %s: %s", uid, exc)
        raise HTTPException(
            status_code=502, detail=f"Microsoft Graph rejected forward: {exc.status_code}",
        ) from exc

    log.info("forward_message: ok user=%s message=%s recipients=%d", uid, message_id, len(payload.to))
    return SendMailOut(ok=True, detail=None)


# ── 1.5 drafts ──────────────────────────────────────────────────────────


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: list[EmailStr] | None = None
    cc: list[EmailStr] | None = None
    subject: str = Field(default="", max_length=998)
    body_html: str = Field(default="", max_length=1_000_000)
    job_id: UUID | None = None


class DraftOut(BaseModel):
    ok: bool
    graph_message_id: str | None = None
    web_link: str | None = None


@router.post(
    "/drafts",
    response_model=DraftOut,
    status_code=201,
    dependencies=[Depends(require_module("email"))],
)
def create_draft(
    payload: DraftIn,
    user: dict[str, Any] = Depends(get_user_for_send),
    control_db: Session = Depends(get_db_for_send),
    tenant_db: Session = Depends(get_db_for_send),
) -> DraftOut:
    """Create a REAL Microsoft Graph draft (1.5).

    ``POST /me/messages`` creates a draft in the user's own mailbox, so it
    round-trips with Outlook and appears in the Drafts folder the rail already
    renders — closing the dead end where Drafts displayed but nothing in GDX
    could create one. (The MCP ``email.draft`` tool's local-only
    ``local-draft-…`` row is a separate, still-local thing.)

    Unlike send, every field is optional: saving a half-written message is the
    entire point of a draft.
    """
    uid, tid = _ids_from_user(user)
    body: dict[str, Any] = {
        "subject": job_marked_subject(
            payload.subject or "", payload.job_id, _job_number(tenant_db, payload.job_id)
        ),
        "body": {"contentType": "html", "content": payload.body_html or ""},
    }
    if payload.to:
        body["toRecipients"] = [{"emailAddress": {"address": str(a)}} for a in payload.to]
    if payload.cc:
        body["ccRecipients"] = [{"emailAddress": {"address": str(a)}} for a in payload.cc]
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            resp = gc._request("POST", "/me/messages", json=body)
    except OutlookReconnectRequired as exc:
        raise HTTPException(
            status_code=409,
            detail="Outlook reconnect required — open Settings → Integrations → Outlook.",
        ) from exc
    except OutlookGraphAPIError as exc:
        log.warning("create_draft: graph error for %s: %s", uid, exc)
        raise HTTPException(
            status_code=502, detail=f"Microsoft Graph rejected draft: {exc.status_code}",
        ) from exc

    # Parsing is guarded SEPARATELY from the call. A blanket except around the
    # send would report ok=True for a connection error too — telling the user
    # their draft is safe in Outlook when nothing was ever sent (audit round
    # 4). Past this point Graph returned 2xx, so the draft genuinely exists
    # and only the id/link are at risk.
    #
    # _request returns an httpx.Response, NOT parsed JSON — every other reader
    # in graph_client calls .json() on it. A MagicMock'd _request hides that,
    # because a mock isn't a dict either.
    try:
        created = resp.json()
    except Exception:  # noqa: BLE001
        log.exception("create_draft: created but response body unreadable")
        return DraftOut(ok=True)

    created = created if isinstance(created, dict) else {}
    log.info("create_draft: ok user=%s id=%s", uid, created.get("id"))
    return DraftOut(
        ok=True,
        graph_message_id=created.get("id"),
        web_link=created.get("webLink"),
    )


# ── P2.4 AI-drafted reply ───────────────────────────────────────────────


class AiDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str | None = Field(default=None, max_length=500)


class AiDraftOut(BaseModel):
    draft_text: str
    source: str  # "ai" | "fallback"


_AI_FALLBACK = (
    "Thanks for reaching out — we got your message and we're on it. "
    "One of us will follow up shortly with next steps.\n\nThank you,\nGarage Door Xperts"
)


@router.post(
    "/messages/{message_id}/ai-draft",
    response_model=AiDraftOut,
    dependencies=[Depends(require_module("email"))],
)
def ai_draft_reply(
    message_id: UUID,
    payload: AiDraftIn,
    user: dict[str, Any] = Depends(get_user_for_send),
    tenant_db: Session = Depends(get_db_for_send),
) -> AiDraftOut:
    """Suggest a reply body for this message (P2.4).

    Lives here rather than reusing ``/api/ai/communication/draft`` for two
    reasons: that route is gated on the *communications* module and requires a
    ``customer_id``, and most inbound mail isn't customer-linked yet. This one
    rides the email module gate and works off what we actually have — subject,
    sender, and the stored preview.

    **This sends customer correspondence to whatever ``AI_PROVIDER_URL``
    points at** (``core/ai_provider.generate_sync``) — sender address, subject,
    and the stored ~255-char preview. Only the preview, never a live-fetched
    full body, but be clear-eyed that a preview routinely carries a name,
    address, or phone number. Messages the owner marked personal are refused
    outright. If AI ever needs a per-tenant opt-in, this is one of the two
    callers to gate.

    The model's output lands in a draft the user reads and sends themselves —
    it is a suggestion, not an auto-reply, which is also what keeps a crafted
    inbound email from steering an outgoing message unreviewed.

    Falls back to a canned acknowledgement when no AI provider is configured,
    so the button never dead-ends.
    """
    uid, _tid = _ids_from_user(user)
    role = (user.get("role") or "viewer").lower()
    msg = tenant_db.query(OutlookMessage).filter(OutlookMessage.id == message_id).one_or_none()
    if msg is None or not can_view(msg, uid, role, tenant_db, tech_emails=_tech_emails(tenant_db)):
        raise HTTPException(status_code=404, detail="message not found")

    # A message the owner marked "🔒 Personal — visible only to you" does not
    # get shipped to an external model, even by the owner's own click.
    if msg.is_personal:
        raise HTTPException(
            status_code=409,
            detail="This message is marked personal — AI drafting is disabled for it.",
        )

    from gdx_dispatch.core.ai_provider import generate_sync  # noqa: PLC0415

    prompt = (
        "Draft a short, professional reply to this email for a garage door "
        "service company. Reply with the message body ONLY — no subject line, "
        "no salutation placeholders like [Name].\n\n"
        f"From: {msg.from_address or 'unknown'}\n"
        f"Subject: {msg.subject or '(no subject)'}\n"
        f"Message: {(msg.body_preview or '').strip()[:1500]}\n"
    )
    if payload.instruction:
        prompt += f"\nExtra instruction from the user: {payload.instruction}\n"
    try:
        result = generate_sync(
            prompt=prompt,
            system=(
                "You are a professional communication assistant for a garage door "
                "service company. Write friendly, concise, specific replies."
            ),
        )
    except Exception:  # noqa: BLE001 — an AI outage must not break compose
        log.exception("ai_draft_reply: provider call failed")
        result = None
    if not result or not str(result).strip():
        return AiDraftOut(draft_text=_AI_FALLBACK, source="fallback")
    return AiDraftOut(draft_text=str(result).strip(), source="ai")
