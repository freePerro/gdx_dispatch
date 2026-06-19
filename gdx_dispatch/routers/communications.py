from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from gdx_dispatch.core import email as email_service
from gdx_dispatch.core import sms as sms_service
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.tenant_ctx import bind_tenant_context, current_tenant_id
from gdx_dispatch.core.twilio_signature import verify_twilio_signature

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["communications"],
    # Task #32: bind_tenant_context first so per-tenant SMS/email inboxes
    # isolate writes across tenants sharing a worker process.
    # get_current_user gates every endpoint here — outbound SMS/email send and
    # message/timeline reads must require an authenticated user (an unauthenticated
    # /api/sms/send is toll-fraud; /timeline leaks customer comms).
    dependencies=[
        Depends(bind_tenant_context),
        Depends(require_module("communications")),
        Depends(get_current_user),
    ],
)

# Inbound provider webhook (Twilio etc.) must be reachable WITHOUT a user session.
# It is intentionally exempt from get_current_user; provider-signature verification
# is the correct auth for it (tracked separately). Everything else stays on `router`.
public_router = APIRouter(
    tags=["communications"],
    dependencies=[
        Depends(bind_tenant_context),
        Depends(require_module("communications")),
    ],
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SMSMessage:
    id: str
    phone: str
    body: str
    direction: str
    created_at: datetime
    status: str
    unread: bool
    job_id: str | None = None


@dataclass
class EmailMessage:
    id: str
    to: str
    subject: str
    body: str
    created_at: datetime
    status: str


_lock = RLock()

# Task #32: per-tenant message buffers, keyed by tenant_id from tenant_ctx
# ContextVar. Real requests bind tenant_id via bind_tenant_context. Code
# paths outside a request context fall through to the '_default' slot.
_SMS_MESSAGES_BY_TENANT: dict[str, list[SMSMessage]] = {}
_EMAILS_BY_TENANT: dict[str, list[EmailMessage]] = {}


def _tenant_sms_messages() -> list[SMSMessage]:
    return _SMS_MESSAGES_BY_TENANT.setdefault(current_tenant_id(), [])


def _tenant_emails() -> list[EmailMessage]:
    return _EMAILS_BY_TENANT.setdefault(current_tenant_id(), [])


# Backward-compatible module-level aliases so existing references continue
# to work after refactor. These proxies always resolve via ContextVar so
# every read/write routes through the current tenant's slot.
class _TenantList:
    def __init__(self, resolver):
        self._resolver = resolver

    def __iter__(self):
        return iter(self._resolver())

    def __len__(self):
        return len(self._resolver())

    def __getitem__(self, idx):
        return self._resolver()[idx]

    def __contains__(self, item):
        return item in self._resolver()

    def append(self, item):
        self._resolver().append(item)

    def clear(self):
        self._resolver().clear()

    def insert(self, idx, item):
        self._resolver().insert(idx, item)

    def remove(self, item):
        self._resolver().remove(item)

    def __bool__(self):
        return bool(self._resolver())


_sms_messages = _TenantList(_tenant_sms_messages)
_emails = _TenantList(_tenant_emails)


async def get_sms_sender() -> Any:
    return sms_service


async def get_email_sender() -> Any:
    return email_service


def reset_state() -> None:
    with _lock:
        _sms_messages.clear()
        _emails.clear()
        _dnc_list.clear()


class SendSMSRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to_phone: str = Field(min_length=1, alias="to")
    body: str = Field(min_length=1)
    from_phone: str | None = None
    tenant_id: str | None = None
    job_id: str | None = None  # backward-compatible metadata


class SendEmailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body_html: str = Field(min_length=1, alias="body")
    from_address: str | None = None
    tenant_branding: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/sms/send", status_code=201)
async def send_sms(
    payload: SendSMSRequest,
    request: Request,
    sender: Any = Depends(get_sms_sender),
) -> dict[str, Any]:
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = payload.tenant_id or str(tenant.get("id") or "unknown")
    from_phone = payload.from_phone or str(os.getenv("TWILIO_PHONE_NUMBER", "")).strip()

    result = sender.send_sms(
        to_phone=payload.to_phone,
        body=payload.body,
        from_phone=from_phone,
        tenant_id=tenant_id,
    )
    sms_id = str(result.get("message_id") or str(uuid.uuid4()))
    status = str(result.get("status") or ("sent" if result.get("sent") else "failed"))

    with _lock:
        _sms_messages.append(
            SMSMessage(
                id=sms_id,
                phone=payload.to_phone,
                body=payload.body,
                direction="outbound",
                created_at=_utcnow(),
                status=status,
                unread=False,
                job_id=payload.job_id,
            )
        )

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="send_sms",
                entity_type="sms",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('send_sms_audit_failed')
    return {
        "id": sms_id,
        "status": status,
        "to": payload.to_phone,
        "body": payload.body,
        "sent": bool(result.get("sent")),
        "reason": result.get("reason"),
        "provider": result.get("provider"),
        "job_id": payload.job_id,
        "tenant_id": tenant_id,
    }


def _parse_inbound_payload(payload: dict[str, Any]) -> tuple[str, str]:
    phone = str(payload.get("From") or payload.get("from") or "").strip()
    body = str(payload.get("Body") or payload.get("body") or "").strip()
    return phone, body


@public_router.post("/api/sms/webhook", response_class=PlainTextResponse)
async def sms_webhook(request: Request, _sig: None = Depends(verify_twilio_signature)) -> PlainTextResponse:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        from_phone, body = _parse_inbound_payload(payload if isinstance(payload, dict) else {})
    else:
        form = await request.form()
        from_phone, body = _parse_inbound_payload(dict(form))

    sms_id = str(uuid.uuid4())
    with _lock:
        _sms_messages.append(
            SMSMessage(
                id=sms_id,
                phone=from_phone,
                body=body,
                direction="inbound",
                created_at=_utcnow(),
                status="received",
                unread=True,
            )
        )

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="sms_webhook",
                entity_type="sms",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('sms_webhook_audit_failed')
    return PlainTextResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="text/xml",
    )


@router.get("/api/sms/conversations")
async def list_sms_conversations() -> list[dict[str, Any]]:
    with _lock:
        grouped: dict[str, dict[str, Any]] = {}
        for msg in _sms_messages:
            convo = grouped.get(msg.phone)
            if convo is None:
                grouped[msg.phone] = {
                    "phone": msg.phone,
                    "last_body": msg.body,
                    "last_at": msg.created_at,
                    "count": 1,
                    "direction": msg.direction,
                }
                continue

            convo["count"] += 1
            if msg.created_at >= convo["last_at"]:
                convo["last_body"] = msg.body
                convo["last_at"] = msg.created_at
                convo["direction"] = msg.direction

        rows = sorted(grouped.values(), key=lambda item: item["last_at"], reverse=True)

    for row in rows:
        row["last_at"] = row["last_at"].isoformat()
    return rows


@router.get("/api/sms/conversations/{phone}")
async def get_sms_conversation(phone: str) -> dict[str, Any]:
    with _lock:
        items = [m for m in _sms_messages if m.phone == phone]
        items.sort(key=lambda m: m.created_at)

    return {
        "phone": phone,
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "body": m.body,
                "timestamp": m.created_at.isoformat(),
                "phone": m.phone,
                "status": m.status,
                "job_id": m.job_id,
            }
            for m in items
        ],
    }


@router.get("/api/inbox/unread-count")
async def unread_count() -> dict[str, int]:
    with _lock:
        count = sum(1 for msg in _sms_messages if msg.direction == "inbound" and msg.unread)
    return {"count": count}


@router.get("/api/inbox/folders")
async def inbox_folders() -> dict[str, list[dict[str, Any]]]:
    with _lock:
        sms_total = len(_sms_messages)
        sms_unread = sum(1 for msg in _sms_messages if msg.direction == "inbound" and msg.unread)
        email_total = len(_emails)

    folders = [
        {"id": "sms", "name": "SMS", "unread": sms_unread, "total": sms_total},
        {"id": "email", "name": "Email", "unread": 0, "total": email_total},
    ]
    return {"folders": folders}


@router.post("/api/email/send", status_code=201)
async def send_email(
    payload: SendEmailRequest,
    sender: Any = Depends(get_email_sender),
) -> dict[str, Any]:
    result = sender.send_email(
        to=payload.to,
        subject=payload.subject,
        body_html=payload.body_html,
        from_address=payload.from_address or "no-reply@example.com",
        tenant_branding=payload.tenant_branding,
    )
    email_id = str(result.get("message_id") or str(uuid.uuid4()))
    status = str(result.get("status") or ("sent" if result.get("sent") else "failed"))

    with _lock:
        _emails.append(
            EmailMessage(
                id=email_id,
                to=payload.to,
                subject=payload.subject,
                body=payload.body_html,
                created_at=_utcnow(),
                status=status,
            )
        )

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="send_email",
                entity_type="email",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('send_email_audit_failed')
    return {
        "id": email_id,
        "status": status,
        "to": payload.to,
        "subject": payload.subject,
        "body": payload.body_html,
        "sent": bool(result.get("sent")),
        "reason": result.get("reason"),
        "provider": result.get("provider"),
    }


# ---------------------------------------------------------------------------
# Unified threads view — SMS conversations + email threads in one feed
# ---------------------------------------------------------------------------
# CommunicationsView.vue hits these three endpoints which were missing before:
#   GET  /api/communications/threads
#   GET  /api/communications/threads/{thread_id}/messages
#   POST /api/communications/send
#
# Threads are synthesized from the in-memory _sms_messages + _emails lists
# with a stable id scheme so the Vue view can navigate between them:
#   sms threads  → id = "sms:{phone}"
#   email threads → id = "email:{to_address}"
#
# This keeps the file's existing in-memory style. A DB-backed rewrite is
# tracked separately in the R&D Operations improvements queue.


def _build_threads() -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    with _lock:
        # Group SMS by phone
        sms_by_phone: dict[str, dict[str, Any]] = {}
        for msg in _sms_messages:
            thread = sms_by_phone.get(msg.phone)
            if thread is None:
                sms_by_phone[msg.phone] = {
                    "id": f"sms:{msg.phone}",
                    "channel": "sms",
                    "subject": f"SMS with {msg.phone}",
                    "customer_id": None,
                    "contact": msg.phone,
                    "last_body": msg.body,
                    "last_at": msg.created_at,
                    "unread_count": 1 if (msg.direction == "inbound" and msg.unread) else 0,
                    "message_count": 1,
                }
                continue
            thread["message_count"] += 1
            if msg.direction == "inbound" and msg.unread:
                thread["unread_count"] += 1
            if msg.created_at >= thread["last_at"]:
                thread["last_body"] = msg.body
                thread["last_at"] = msg.created_at
        threads.extend(sms_by_phone.values())

        # Group email by recipient
        email_by_to: dict[str, dict[str, Any]] = {}
        for em in _emails:
            thread = email_by_to.get(em.to)
            if thread is None:
                email_by_to[em.to] = {
                    "id": f"email:{em.to}",
                    "channel": "email",
                    "subject": em.subject,
                    "customer_id": None,
                    "contact": em.to,
                    "last_body": em.body[:200],
                    "last_at": em.created_at,
                    "unread_count": 0,
                    "message_count": 1,
                }
                continue
            thread["message_count"] += 1
            if em.created_at >= thread["last_at"]:
                thread["subject"] = em.subject
                thread["last_body"] = em.body[:200]
                thread["last_at"] = em.created_at
        threads.extend(email_by_to.values())

    threads.sort(key=lambda t: t["last_at"], reverse=True)
    # Serialize datetimes for JSON
    for t in threads:
        t["last_at"] = t["last_at"].isoformat() if hasattr(t["last_at"], "isoformat") else t["last_at"]
    return threads


@router.get("/api/communications/threads")
async def list_communication_threads(
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Unified threads list for CommunicationsView.vue."""
    threads = _build_threads()
    if channel:
        threads = [t for t in threads if t["channel"] == channel]
    if search:
        needle = search.lower()
        threads = [
            t for t in threads
            if needle in (t.get("subject") or "").lower()
            or needle in (t.get("contact") or "").lower()
            or needle in (t.get("last_body") or "").lower()
        ]
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    total = len(threads)
    start = (page - 1) * page_size
    return {
        "items": threads[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/api/communications/threads/{thread_id}/messages")
async def get_thread_messages(thread_id: str) -> dict[str, Any]:
    """Return all messages in one synthetic thread (SMS phone or email recipient)."""
    if ":" not in thread_id:
        return {"items": [], "total": 0}
    kind, key = thread_id.split(":", 1)
    messages: list[dict[str, Any]] = []
    with _lock:
        if kind == "sms":
            for m in _sms_messages:
                if m.phone != key:
                    continue
                messages.append({
                    "id": m.id,
                    "thread_id": thread_id,
                    "channel": "sms",
                    "direction": m.direction,
                    "from_addr": None if m.direction == "outbound" else m.phone,
                    "to_addr": m.phone if m.direction == "outbound" else None,
                    "subject": None,
                    "body": m.body,
                    "status": m.status,
                    "created_at": m.created_at.isoformat(),
                })
        elif kind == "email":
            for em in _emails:
                if em.to != key:
                    continue
                messages.append({
                    "id": em.id,
                    "thread_id": thread_id,
                    "channel": "email",
                    "direction": "outbound",
                    "from_addr": None,
                    "to_addr": em.to,
                    "subject": em.subject,
                    "body": em.body,
                    "status": em.status,
                    "created_at": em.created_at.isoformat(),
                })
    messages.sort(key=lambda x: x["created_at"])
    return {"items": messages, "total": len(messages), "thread_id": thread_id}


class SendCommunicationRequest(BaseModel):
    """Unified send — Vue view posts one shape for either channel."""
    model_config = ConfigDict(populate_by_name=True)

    channel: str = Field(..., pattern="^(sms|email)$")
    to: str = Field(..., min_length=1)
    subject: str | None = None
    body: str = Field(..., min_length=1)
    thread_id: str | None = None
    customer_id: str | None = None


@router.post("/api/communications/send", status_code=201)
async def send_communication(
    payload: SendCommunicationRequest,
    request: Request,
    sms_sender: Any = Depends(get_sms_sender),
    email_sender: Any = Depends(get_email_sender),
) -> dict[str, Any]:
    """Unified outbound send. Dispatches to the right provider by channel and
    records the message in the in-memory log so the threads view picks it up."""
    now = _utcnow()
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", "")) or "unknown"
    if payload.channel == "sms":
        result = sms_sender.send_sms(
            to_phone=payload.to,
            body=payload.body,
            from_phone=os.getenv("GDX_SMS_FROM", "+15555550100"),
            tenant_id=tenant_id,
        )
        message_id = str(result.get("message_id") or str(uuid.uuid4()))
        status = str(result.get("status") or ("sent" if result.get("sent") else "failed"))
        with _lock:
            _sms_messages.append(
                SMSMessage(
                    id=message_id,
                    phone=payload.to,
                    body=payload.body,
                    direction="outbound",
                    created_at=now,
                    status=status,
                    unread=False,
                )
            )
        return {
            "id": message_id,
            "thread_id": f"sms:{payload.to}",
            "channel": "sms",
            "status": status,
            "created_at": now.isoformat(),
        }
    else:  # email
        result = email_sender.send_email(
            to=payload.to,
            subject=payload.subject or "(no subject)",
            body_html=payload.body,
            from_address="no-reply@example.com",
            tenant_branding={},
        )
        message_id = str(result.get("message_id") or str(uuid.uuid4()))
        status = str(result.get("status") or ("sent" if result.get("sent") else "failed"))
        with _lock:
            _emails.append(
                EmailMessage(
                    id=message_id,
                    to=payload.to,
                    subject=payload.subject or "(no subject)",
                    body=payload.body,
                    created_at=now,
                    status=status,
                )
            )
        # TODO(audit): verify action/entity_type/entity_id/details for this handler
        _audit_db = locals().get('db')
        if _audit_db is not None:
            try:
                _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
                _audit_req = locals().get('request')
                _audit_tenant = ''
                if _audit_req is not None:
                    _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
                _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
                log_audit_event_sync(
                    _audit_db,
                    tenant_id=_audit_tenant,
                    user_id=_audit_user,
                    action="        ",
                    entity_type="communication",
                    entity_id="",
                    details={},
                    request=_audit_req,
                )
                _audit_db.commit()
            except Exception:
                log.exception('        ')
        return {
            "id": message_id,
            "thread_id": f"email:{payload.to}",
            "channel": "email",
            "status": status,
            "created_at": now.isoformat(),
        }


# ---------------------------------------------------------------------------
# Communication Timeline (#189) — all messages for a customer
# ---------------------------------------------------------------------------

@router.get("/api/communications/timeline/{customer_id}")
def get_customer_timeline(customer_id: str) -> list[dict[str, Any]]:
    """Return chronological timeline of all SMS + email for a customer."""
    timeline: list[dict[str, Any]] = []

    # Collect SMS messages
    for msg in _sms_messages:
        timeline.append({
            "id": msg.id,
            "type": "sms",
            "direction": msg.direction,
            "body": msg.body,
            "phone": msg.phone,
            "created_at": msg.created_at.isoformat(),
        })

    # Collect emails
    for email in _emails:
        timeline.append({
            "id": email.id,
            "type": "email",
            "direction": "outbound",
            "body": email.subject,
            "to": email.to,
            "created_at": email.created_at.isoformat(),
        })

    timeline.sort(key=lambda x: x["created_at"], reverse=True)
    return timeline


# ---------------------------------------------------------------------------
# Do-Not-Contact Flag (#190) — opt-out management
# ---------------------------------------------------------------------------

# Task #32: per-tenant DNC set, resolved via ContextVar.
_DNC_LIST_BY_TENANT: dict[str, set[str]] = {}


def _tenant_dnc_list() -> set[str]:
    return _DNC_LIST_BY_TENANT.setdefault(current_tenant_id(), set())


class _TenantSet:
    def __init__(self, resolver):
        self._resolver = resolver

    def __iter__(self):
        return iter(self._resolver())

    def __len__(self):
        return len(self._resolver())

    def __contains__(self, item):
        return item in self._resolver()

    def add(self, item):
        self._resolver().add(item)

    def discard(self, item):
        self._resolver().discard(item)

    def clear(self):
        self._resolver().clear()

    def __bool__(self):
        return bool(self._resolver())


_dnc_list = _TenantSet(_tenant_dnc_list)


class DNCRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    channel: str = Field(default="all", pattern="^(all|sms|email)$")


@router.post("/api/communications/dnc")
def add_to_dnc(payload: DNCRequest) -> dict[str, Any]:
    """Add a customer to the do-not-contact list."""
    key = f"{payload.customer_id}:{payload.channel}"
    _dnc_list.add(key)
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="add_to_dnc",
                entity_type="dnc_list",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('add_to_dnc_audit_failed')
    return {"customer_id": payload.customer_id, "channel": payload.channel, "dnc": True}


@router.delete("/api/communications/dnc/{customer_id}")
def remove_from_dnc(customer_id: str, channel: str = "all") -> dict[str, Any]:
    """Remove a customer from the do-not-contact list."""
    key = f"{customer_id}:{channel}"
    _dnc_list.discard(key)
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="remove_from_dnc",
                entity_type="dnc_list",
                entity_id=str(customer_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('remove_from_dnc_audit_failed')
    return {"customer_id": customer_id, "channel": channel, "dnc": False}


@router.get("/api/communications/dnc/{customer_id}")
def check_dnc(customer_id: str) -> dict[str, Any]:
    """Check if a customer is on the do-not-contact list."""
    blocked_channels = [k.split(":")[1] for k in _dnc_list if k.startswith(f"{customer_id}:")]
    return {"customer_id": customer_id, "blocked_channels": blocked_channels, "is_blocked": len(blocked_channels) > 0}
