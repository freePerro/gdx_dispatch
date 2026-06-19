"""Sprint tech_mobile Phase 4.1 — Per-job dispatch chat (REST polling v1).

Flat thread keyed by job_id. v1 uses REST polling (GET on a 5–10s
cadence from the client); a Starlette WebSocket + Redis pub/sub
upgrade is reserved for v2 once we have real chat traffic to justify
the moving parts.

Endpoints (all under /api/mobile, gated on the "mobile" module):

    GET  /api/mobile/jobs/{job_id}/chat
        ?since=<iso8601>   — only messages newer than this stamp
        ?limit=N           — newest N (default 200)
    POST /api/mobile/jobs/{job_id}/chat
        body: { kind: text|quick_action, body, quick_action? }
    POST /api/mobile/chat/{message_id}/read
        — dispatcher-only; stamps read_by + read_at
    GET  /api/mobile/dispatch/threads
        — dispatcher only; lists active threads sorted unread-first
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID as _UUID
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as _text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import JobChatMessage

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except Exception:
    log.exception("mobile_chat_auth_import_failed_using_fallback")

    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile-chat"],
    dependencies=[Depends(require_module("mobile"))],
)


def _jr(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


def _tenant_id(request: Request) -> str:
    state = getattr(request, "state", None)
    tenant = getattr(state, "tenant", None) or {}
    return str(tenant.get("id") or getattr(state, "tenant_id", "") or "")


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "")


def _role(user: dict[str, Any]) -> str:
    return str(user.get("role") or "").lower()


def _is_dispatcher(user: dict[str, Any]) -> bool:
    role = _role(user)
    return role in {"dispatcher", "admin", "owner"}


def _job_visible_to_user(db: Session, job_id: str, user: dict[str, Any]) -> bool:
    """Tech sees only their own jobs; dispatchers see all jobs in tenant."""
    if _is_dispatcher(user):
        row = db.execute(
            _text("SELECT 1 FROM jobs WHERE id = :jid AND deleted_at IS NULL LIMIT 1"),
            {"jid": job_id},
        ).scalar()
        return bool(row)
    uid = _user_id(user)
    if not uid:
        return False
    row = db.execute(
        _text(
            """
            SELECT 1 FROM jobs
            WHERE id = :jid AND deleted_at IS NULL AND assigned_to = :uid
            LIMIT 1
            """
        ),
        {"jid": job_id, "uid": uid},
    ).scalar()
    if row:
        return True
    row = db.execute(
        _text(
            """
            SELECT 1 FROM job_assignments ja
            JOIN technicians t ON t.id = ja.tech_id
            WHERE ja.job_id = :jid AND CAST(t.user_id AS TEXT) = :uid AND t.active IS NOT FALSE
            LIMIT 1
            """
        ),
        {"jid": job_id, "uid": uid},
    ).scalar()
    return bool(row)


# Canonical quick-action set. Stored as `quick_action` slug + `body` is
# the human-readable label so older clients without slug awareness still
# render correctly.
QUICK_ACTIONS = {
    "on_my_way": "On my way",
    "customer_not_home": "Customer not home",
    "running_late": "Job will run long",
    "send_another_tech": "Need another tech",
    "need_help": "Need help here",
    "blocked": "Blocked — see notes",
}


def _serialize_message(m: JobChatMessage) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "job_id": m.job_id,
        "sender_user_id": m.sender_user_id,
        "sender_role": m.sender_role,
        "sender_name": m.sender_name,
        "kind": m.kind,
        "body": m.body,
        "quick_action": m.quick_action,
        "read_by_user_id": m.read_by_user_id,
        "read_at": m.read_at.isoformat() if m.read_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# ---------------------------------------------------------------------------
# Pydantic input shapes
# ---------------------------------------------------------------------------


class SendChatIn(BaseModel):
    kind: str = Field(default="text", pattern="^(text|quick_action|photo)$")
    body: str | None = Field(default=None, max_length=5000)
    quick_action: str | None = Field(default=None, max_length=40)


# ---------------------------------------------------------------------------
# GET /api/mobile/jobs/{job_id}/chat
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/chat", response_model=None)
def get_job_chat(
    job_id: str,
    request: Request,
    since: str | None = Query(default=None, description="ISO8601; only newer messages"),
    limit: int = Query(default=200, ge=1, le=500),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = current_user or {}
    if not _job_visible_to_user(db, job_id, user):
        return _jr({"detail": "job not found or not visible"}, 404)

    sql = """
        SELECT id, company_id, job_id, sender_user_id, sender_role, sender_name,
               kind, body, quick_action, read_by_user_id, read_at, created_at
        FROM job_chat_messages
        WHERE job_id = :jid AND deleted_at IS NULL
    """
    params: dict[str, Any] = {"jid": job_id, "limit": limit}
    if since and isinstance(since, str):
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            sql += " AND created_at > :since"
            params["since"] = since_dt
        except ValueError:
            return _jr({"detail": "invalid since timestamp"}, 400)
    # Coerce limit when called outside FastAPI (tests pass the raw default).
    if not isinstance(limit, int):
        limit = 200
    params["limit"] = limit
    sql += " ORDER BY created_at DESC LIMIT :limit"
    rows = db.execute(_text(sql), params).all()
    # Reverse to chronological for client convenience.
    rows = list(reversed(rows))
    return _jr({
        "job_id": job_id,
        "messages": [
            {
                "id": str(r[0]),
                "sender_user_id": r[3],
                "sender_role": r[4],
                "sender_name": r[5],
                "kind": r[6],
                "body": r[7],
                "quick_action": r[8],
                "read_by_user_id": r[9],
                "read_at": r[10].isoformat() if hasattr(r[10], "isoformat") else r[10],
                "created_at": r[11].isoformat() if hasattr(r[11], "isoformat") else r[11],
            }
            for r in rows
        ],
        "quick_actions": QUICK_ACTIONS,
    })


# ---------------------------------------------------------------------------
# POST /api/mobile/jobs/{job_id}/chat
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/chat", response_model=None, status_code=201)
def send_job_chat(
    job_id: str,
    payload: SendChatIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not user_id:
        return _jr({"detail": "no user"}, 401)
    if not _job_visible_to_user(db, job_id, user):
        return _jr({"detail": "job not found or not visible"}, 404)

    # Resolve body.
    body = (payload.body or "").strip()
    quick = (payload.quick_action or "").strip() or None
    if payload.kind == "quick_action":
        if not quick or quick not in QUICK_ACTIONS:
            return _jr({"detail": f"unknown quick_action; allowed: {list(QUICK_ACTIONS)}"}, 400)
        body = QUICK_ACTIONS[quick]
    elif payload.kind == "text":
        if not body:
            return _jr({"detail": "body required for text messages"}, 400)
    elif payload.kind == "photo":
        return _jr({"detail": "photo attachments deferred to v2"}, 501)

    role = "dispatcher" if _is_dispatcher(user) else "tech"
    name = user.get("name") or user.get("display_name") or user.get("email")
    msg = JobChatMessage(
        id=uuid4(),
        company_id=str(tenant_id),
        job_id=job_id,
        sender_user_id=user_id,
        sender_role=role,
        sender_name=name,
        kind=payload.kind,
        body=body,
        quick_action=quick,
        created_at=datetime.now(UTC),
    )
    db.add(msg)
    db.commit()

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_chat_sent",
        entity_type="job_chat_message",
        entity_id=str(msg.id),
        details={"job_id": job_id, "kind": payload.kind, "quick_action": quick},
        request=request,
    )
    db.commit()

    # Best-effort push notification to the other party.
    try:
        _push_other_party(db, job_id=job_id, msg=msg, user=user, request=request)
    except Exception:
        log.exception("mobile_chat_push_failed msg=%s", msg.id)

    return _jr(_serialize_message(msg), 201)


def _push_other_party(db: Session, *, job_id: str, msg: JobChatMessage, user: dict[str, Any], request: Request) -> None:
    """Notify the recipient(s).

    Tech sent → push every dispatcher-role user in the tenant.
    Dispatcher sent → push the assigned tech (assigned_to + job_assignments).
    """
    try:
        from gdx_dispatch.core.push_subscriptions import send_push  # type: ignore[attr-defined]
    except Exception:
        return  # push infra not configured; chat still works in-app

    title = "Job message"
    body = (msg.body or "").strip()[:140]
    url = f"/mobile?job={job_id}"
    if msg.sender_role in ("dispatcher", "admin", "owner"):
        # Push the assigned tech(s).
        rows = db.execute(
            _text(
                """
                SELECT DISTINCT user_id FROM (
                  SELECT assigned_to AS user_id FROM jobs WHERE id = :jid AND assigned_to IS NOT NULL
                  UNION
                  SELECT t.user_id FROM job_assignments ja
                  JOIN technicians t ON t.id = ja.tech_id
                  WHERE ja.job_id = :jid AND t.active IS NOT FALSE
                ) s WHERE user_id IS NOT NULL
                """
            ),
            {"jid": job_id},
        ).all()
        for r in rows:
            try:
                send_push(db, user_id=r[0], title=title, body=body, url=url,
                          data={"type": "chat_message", "job_id": job_id})
            except Exception:
                log.exception("send_push failed user=%s", r[0])
    else:
        # Tech sent → notify dispatcher(s). User-role lookup needs the
        # control plane; cheap fallback: look up users whose tenant role
        # is dispatcher in the user_roles table if it exists.
        try:
            rows = db.execute(
                _text(
                    """
                    SELECT DISTINCT user_id FROM user_role_assignments ura
                    JOIN tenant_roles r ON r.id = ura.role_id
                    WHERE r.name IN ('dispatcher','admin','owner')
                    """
                )
            ).all()
            for r in rows:
                try:
                    send_push(db, user_id=r[0], title=title, body=body, url=url,
                              data={"type": "chat_message", "job_id": job_id})
                except Exception:
                    log.exception("send_push failed user=%s", r[0])
        except Exception:
            # role tables not present in this tenant DB — silently skip.
            pass


# ---------------------------------------------------------------------------
# POST /api/mobile/chat/{message_id}/read  (dispatcher-only)
# ---------------------------------------------------------------------------


@router.post("/chat/{message_id}/read", response_model=None)
def mark_chat_read(
    message_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = current_user or {}
    if not _is_dispatcher(user):
        return _jr({"detail": "only dispatchers stamp read receipts"}, 403)
    user_id = _user_id(user)
    msg = db.execute(
        select(JobChatMessage).where(
            JobChatMessage.id == _UUID(message_id),
            JobChatMessage.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if msg is None:
        return _jr({"detail": "message not found"}, 404)
    if msg.read_at is None:
        msg.read_by_user_id = user_id
        msg.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(msg)
    return _jr(_serialize_message(msg))


# ---------------------------------------------------------------------------
# GET /api/mobile/dispatch/threads  (dispatcher-only)
# ---------------------------------------------------------------------------


@router.get("/dispatch/threads", response_model=None)
def list_dispatch_threads(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = current_user or {}
    if not _is_dispatcher(user):
        return _jr({"detail": "dispatcher-only"}, 403)
    # Active threads: distinct job_ids with messages in the last 7 days,
    # unread-first (read_at IS NULL on the latest tech-sent message).
    rows = db.execute(
        _text(
            """
            SELECT m.job_id,
                   MAX(m.created_at) AS last_at,
                   SUM(CASE WHEN m.sender_role = 'tech' AND m.read_at IS NULL THEN 1 ELSE 0 END) AS unread,
                   COUNT(*) AS total
            FROM job_chat_messages m
            WHERE m.deleted_at IS NULL
              AND m.created_at > (datetime('now', '-7 days'))
            GROUP BY m.job_id
            ORDER BY unread DESC, last_at DESC
            LIMIT 200
            """
        )
    ).all() if db.bind.dialect.name == "sqlite" else db.execute(
        _text(
            """
            SELECT m.job_id,
                   MAX(m.created_at) AS last_at,
                   SUM(CASE WHEN m.sender_role = 'tech' AND m.read_at IS NULL THEN 1 ELSE 0 END) AS unread,
                   COUNT(*) AS total
            FROM job_chat_messages m
            WHERE m.deleted_at IS NULL
              AND m.created_at > (now() - INTERVAL '7 days')
            GROUP BY m.job_id
            ORDER BY unread DESC, last_at DESC
            LIMIT 200
            """
        )
    ).all()
    threads = []
    for r in rows:
        # Pull the job + customer details for context.
        jrow = db.execute(
            _text(
                """
                SELECT j.title, c.name, c.address
                FROM jobs j
                LEFT JOIN customers c ON c.id = j.customer_id
                WHERE j.id = :jid
                """
            ),
            {"jid": r[0]},
        ).first()
        threads.append({
            "job_id": r[0],
            "last_message_at": r[1].isoformat() if hasattr(r[1], "isoformat") else r[1],
            "unread_count": int(r[2] or 0),
            "total_count": int(r[3] or 0),
            "job_title": jrow[0] if jrow else None,
            "customer_name": jrow[1] if jrow else None,
            "customer_address": jrow[2] if jrow else None,
        })
    return _jr({"threads": threads})
