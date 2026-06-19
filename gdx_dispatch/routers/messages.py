"""
Team messages router — internal team-to-team messaging within a tenant.

Not customer-facing. Supports per-recipient read status, unread counter for a
user menu bell, and a flat (thread-less) ordered conversation.

Gated behind the "jobs" module. Follows the notes.py pattern for tenant
scoping, author-gated delete (sender or admin), and audit logging.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    and_,
    func,
    select,
)
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["team_messages"],
    dependencies=[Depends(require_module("jobs"))],
)


ADMIN_ROLES = ("admin", "owner")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import TeamMessage, TeamMessageRecipient  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class MessageIn(BaseModel):
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=10000)
    recipient_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("recipient_ids")
    @classmethod
    def _bound_each_recipient(cls, v: list[str]) -> list[str]:
        for r in v:
            if not r or len(r) > 200:
                raise ValueError("each recipient_id must be 1-200 chars")
        return list(dict.fromkeys(v))  # dedupe preserving order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _user_name(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("name") or user.get("email") or None


def _user_role(user: Any) -> str:
    if not isinstance(user, dict):
        return ""
    return str(user.get("role") or "")


def _preview(body: str, limit: int = 200) -> str:
    if body is None:
        return ""
    b = body.strip()
    return b if len(b) <= limit else b[:limit] + "..."


def _serialize_inbox_row(msg: TeamMessage, rcpt: TeamMessageRecipient) -> dict[str, Any]:
    return {
        "id": str(msg.id),
        "company_id": msg.company_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "subject": msg.subject,
        "body": msg.body,
        "preview": _preview(msg.body),
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "read_at": rcpt.read_at.isoformat() if rcpt.read_at else None,
        "recipient_row_id": str(rcpt.id),
    }


def _serialize_sent(
    msg: TeamMessage, recipients: list[TeamMessageRecipient]
) -> dict[str, Any]:
    return {
        "id": str(msg.id),
        "company_id": msg.company_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "subject": msg.subject,
        "body": msg.body,
        "preview": _preview(msg.body),
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "recipients": [
            {
                "recipient_id": r.recipient_id,
                "read_at": r.read_at.isoformat() if r.read_at else None,
            }
            for r in recipients
        ],
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type="team_message",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception(
            "team_message_audit_failed action=%s entity_id=%s", action, entity_id
        )
        db.rollback()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/messages", response_model=None)
def list_inbox(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    me = _user_id(user)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(TeamMessage, TeamMessageRecipient)
        .join(
            TeamMessageRecipient,
            TeamMessageRecipient.message_id == TeamMessage.id,
        )
        .where(
            TeamMessageRecipient.recipient_id == me,
            TeamMessage.deleted_at.is_(None),
        )
        .order_by(TeamMessage.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        stmt = stmt.where(TeamMessageRecipient.read_at.is_(None))

    rows = db.execute(stmt).all()
    return [_serialize_inbox_row(m, r) for (m, r) in rows]


@router.get("/api/messages/unread_count", response_model=None)
def unread_count(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    tenant_id = _tenant_id(request)
    me = _user_id(user)
    stmt = (
        select(func.count(TeamMessageRecipient.id))
        .join(
            TeamMessage,
            and_(
                TeamMessage.id == TeamMessageRecipient.message_id,
                TeamMessage.company_id == TeamMessageRecipient.company_id,
            ),
        )
        .where(
            TeamMessageRecipient.company_id == tenant_id,
            TeamMessageRecipient.recipient_id == me,
            TeamMessageRecipient.read_at.is_(None),
            TeamMessage.deleted_at.is_(None),
        )
    )
    count = db.execute(stmt).scalar() or 0
    return {"count": int(count)}


@router.get("/api/messages/sent", response_model=None)
def list_sent(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    me = _user_id(user)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    msg_stmt = (
        select(TeamMessage)
        .where(
            TeamMessage.sender_id == me,
            TeamMessage.deleted_at.is_(None),
        )
        .order_by(TeamMessage.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    messages = db.execute(msg_stmt).scalars().all()
    if not messages:
        return []

    msg_ids = [m.id for m in messages]
    rcpt_rows = (
        db.execute(
            select(TeamMessageRecipient).where(
                TeamMessageRecipient.message_id.in_(msg_ids),
            )
        )
        .scalars()
        .all()
    )
    by_msg: dict[UUID, list[TeamMessageRecipient]] = {}
    for r in rcpt_rows:
        by_msg.setdefault(r.message_id, []).append(r)

    return [_serialize_sent(m, by_msg.get(m.id, [])) for m in messages]


@router.post("/api/messages", response_model=None, status_code=201)
def send_message(
    payload: MessageIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    sender_id = _user_id(user)

    msg = TeamMessage(
        company_id=tenant_id,
        sender_id=sender_id,
        sender_name=_user_name(user),
        subject=payload.subject,
        body=payload.body,
    )
    db.add(msg)
    db.flush()  # ensure msg.id is populated

    recipient_rows: list[TeamMessageRecipient] = []
    for rid in payload.recipient_ids:
        r = TeamMessageRecipient(
            company_id=tenant_id,
            message_id=msg.id,
            recipient_id=rid,
        )
        db.add(r)
        recipient_rows.append(r)

    db.commit()
    db.refresh(msg)
    for r in recipient_rows:
        db.refresh(r)

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="team_message_sent",
        entity_id=str(msg.id),
        details={
            "recipient_count": len(recipient_rows),
            "subject": msg.subject,
        },
        request=request,
    )
    return _serialize_sent(msg, recipient_rows)


@router.patch("/api/messages/{message_id}/read", response_model=None, status_code=204)
def mark_read(
    message_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    me = _user_id(user)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    row = db.execute(
        select(TeamMessageRecipient).where(
            TeamMessageRecipient.message_id == message_id,
            TeamMessageRecipient.recipient_id == me,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")

    if row.read_at is None:
        row.read_at = utcnow()
        db.commit()
        _audit(
            db,
            tenant_id=tenant_id,
            user=user,
            action="team_message_read",
            entity_id=str(message_id),
            details={},
            request=request,
        )
    return None


@router.post("/api/messages/mark-all-read", response_model=None)
def mark_all_read(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    tenant_id = _tenant_id(request)
    me = _user_id(user)
    now = utcnow()

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = (
        db.execute(
            select(TeamMessageRecipient).where(
                TeamMessageRecipient.recipient_id == me,
                TeamMessageRecipient.read_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    marked = 0
    for r in rows:
        r.read_at = now
        marked += 1
    db.commit()

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="team_message_marked_all_read",
        entity_id="",
        details={"marked": marked},
        request=request,
    )
    return {"marked": marked}


@router.delete("/api/messages/{message_id}", response_model=None, status_code=204)
def delete_message(
    message_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    msg = db.execute(
        select(TeamMessage).where(
            TeamMessage.id == message_id,
            TeamMessage.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    me = _user_id(user)
    role = _user_role(user)
    if msg.sender_id != me and role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403, detail="Only the sender or an admin can delete"
        )

    msg.deleted_at = utcnow()
    db.commit()

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="team_message_deleted",
        entity_id=str(message_id),
        details={"sender_id": msg.sender_id},
        request=request,
    )
    return None
