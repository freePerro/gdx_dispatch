"""Sprint Outlook Integration — Phase 6 send endpoint.

``POST /api/outlook/send`` — sends an email AS the current user via
Microsoft Graph ``/me/sendMail``. The sent message comes back via the
existing webhook + delta sync pipeline (Phase 2), so no separate "sent
folder sync" path is needed.

Compose UI (Vue) lives in slice S32 — backend only here.
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


def get_db_for_send(db: Session = Depends(get_db)) -> Session:
    return db


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


def _build_graph_body(payload: SendMailIn) -> dict:
    """Translate our SendMailIn into the Graph /me/sendMail wire format.

    Used for new conversations only. Replies go via /me/messages/{id}/reply
    so Graph wires In-Reply-To + References headers itself.
    """
    msg = {
        "subject": payload.subject,
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
    """Body for POST /me/messages/{id}/reply — Graph adds threading headers."""
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
            if parent is not None and parent.graph_message_id:
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
                gc._request("POST", "/me/sendMail", json=_build_graph_body(payload))
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
