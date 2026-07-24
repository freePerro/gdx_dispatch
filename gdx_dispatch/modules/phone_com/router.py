"""Sprint phone-com pc-s9/s10/s11 — operational router.

Tenant-plane endpoints under ``/api/phone-com``. Auth + module gate
mandatory on every route. Connection-isolated tenant DB (no tenant_id
filtering — per CLAUDE.md three-plane).

The audio-proxy endpoints stream from Phone.com servers without
exposing the upstream URL to the caller. The cp_url path skips Bearer
auth (presigned); the authed-url path uses the per-tenant Bearer token.

Slices covered:
- pc-s9 calls: list/detail/recording/voicemail-audio/voicemail-transcript/mark-heard
- pc-s10 messages: list-threads/list-by-thread/send/inbound counters
- pc-s11 stats + catalog: dashboard summary + extensions/numbers list
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc, func
from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Job
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import PhoneComAPIError, PhoneComClient
from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComExtension,
    PhoneComFax,
    PhoneComMessage,
    PhoneComNumber,
    PhoneComStatsDaily,
    PhoneComVoicemail,
)

log = logging.getLogger("gdx_dispatch.modules.phone_com.router")

router = APIRouter(
    prefix="/api/phone-com",
    tags=["phone-com"],
    dependencies=[Depends(require_module("phone_com"))],
)


# ── tenant client construction ─────────────────────────────────────────


def _get_phone_com_client(
    tenant_id: UUID, control_db: Session, tenant_db: Session
) -> PhoneComClient:
    token = key_storage.get_token(control_db, tenant_id)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Phone.com integration not configured",
        )
    app = tenant_db.query(AppSettings).first()
    voip_id_raw = app.phone_com_voip_id if app else None
    if voip_id_raw is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Phone.com voip_id not set — re-run /test on the integration",
        )
    try:
        voip_id = int(voip_id_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=503, detail="Phone.com voip_id is malformed"
        ) from None
    return PhoneComClient(token=token, voip_id=voip_id)


def _coerce_tenant_uuid(user: dict[str, Any]) -> UUID:
    tid = user.get("tenant_id")
    if not tid:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return tid if isinstance(tid, UUID) else UUID(str(tid))


def _coerce_user_uuid(user: dict[str, Any]) -> UUID | None:
    uid = user.get("user_id") or user.get("id") or user.get("sub")
    if uid is None:
        return None
    if isinstance(uid, UUID):
        return uid
    try:
        return UUID(str(uid))
    except (TypeError, ValueError):
        return None


def _account_features(tenant_db: Session) -> dict[str, Any]:
    app = tenant_db.query(AppSettings).first()
    return (app.phone_com_account_features if app else None) or {}


# ── pc-s9: calls ───────────────────────────────────────────────────────


class CallListItem(BaseModel):
    id: UUID
    direction: str
    from_number: str | None
    caller_cnam: str | None
    to_number: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: int | None
    status: str | None
    extension_id: str | None
    customer_id: UUID | None
    customer_name: str | None
    job_id: UUID | None
    has_voicemail: bool
    has_recording: bool


class CallListOut(BaseModel):
    items: list[CallListItem]
    total: int
    page: int
    per_page: int


def _voicemail_index(tenant_db: Session, call_ids: list[UUID]) -> dict[UUID, PhoneComVoicemail]:
    if not call_ids:
        return {}
    rows = (
        tenant_db.query(PhoneComVoicemail)
        .filter(PhoneComVoicemail.call_id.in_(call_ids))
        .all()
    )
    return {row.call_id: row for row in rows if row.call_id is not None}


def _customer_name_index(tenant_db: Session, customer_ids: list[UUID]) -> dict[UUID, str]:
    if not customer_ids:
        return {}
    rows = tenant_db.query(Customer.id, Customer.name).filter(
        Customer.id.in_(customer_ids)
    ).all()
    return {row[0]: row[1] for row in rows}


def _call_to_list_item(
    call: PhoneComCall,
    *,
    voicemails: dict[UUID, PhoneComVoicemail],
    customer_names: dict[UUID, str],
    recording_feature_on: bool,
) -> CallListItem:
    vm = voicemails.get(call.id)
    raw = call.raw_payload or {}
    return CallListItem(
        id=call.id,
        direction=call.direction,
        from_number=call.from_number,
        caller_cnam=raw.get("caller_cnam") if isinstance(raw, dict) else None,
        to_number=call.to_number,
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_s=call.duration_s,
        status=call.status,
        extension_id=call.extension_id,
        customer_id=call.customer_id,
        customer_name=customer_names.get(call.customer_id) if call.customer_id else None,
        job_id=call.job_id,
        has_voicemail=vm is not None and bool(vm.audio_url or vm.transcript),
        has_recording=bool(call.recording_url) and recording_feature_on,
    )


@router.get("/calls", response_model=CallListOut)
def list_calls(
    customer_id: UUID | None = None,
    job_id: UUID | None = None,
    direction: str | None = Query(default=None, pattern="^(in|out)$"),
    from_date: datetime | None = Query(default=None, alias="from"),
    to_date: datetime | None = Query(default=None, alias="to"),
    has_voicemail: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001 — auth gate
) -> CallListOut:
    q = tenant_db.query(PhoneComCall)
    if customer_id is not None:
        q = q.filter(PhoneComCall.customer_id == customer_id)
    if job_id is not None:
        q = q.filter(PhoneComCall.job_id == job_id)
    if direction is not None:
        q = q.filter(PhoneComCall.direction == direction)
    if from_date is not None:
        q = q.filter(PhoneComCall.started_at >= from_date)
    if to_date is not None:
        q = q.filter(PhoneComCall.started_at <= to_date)

    if has_voicemail is True:
        # subquery for calls with a voicemail row
        vm_call_ids = tenant_db.query(PhoneComVoicemail.call_id).filter(
            PhoneComVoicemail.call_id.isnot(None)
        )
        q = q.filter(PhoneComCall.id.in_(vm_call_ids))
    elif has_voicemail is False:
        vm_call_ids = tenant_db.query(PhoneComVoicemail.call_id).filter(
            PhoneComVoicemail.call_id.isnot(None)
        )
        q = q.filter(~PhoneComCall.id.in_(vm_call_ids))

    total = q.count()
    rows = (
        q.order_by(desc(PhoneComCall.started_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    voicemails = _voicemail_index(tenant_db, [r.id for r in rows])
    customer_ids = [r.customer_id for r in rows if r.customer_id]
    customer_names = _customer_name_index(tenant_db, customer_ids)
    rec_on = bool(_account_features(tenant_db).get("call-recording-on"))

    return CallListOut(
        items=[
            _call_to_list_item(r, voicemails=voicemails,
                               customer_names=customer_names,
                               recording_feature_on=rec_on)
            for r in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/calls/{call_id}")
def get_call_detail(
    call_id: UUID,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    call = tenant_db.get(PhoneComCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    vm = (
        tenant_db.query(PhoneComVoicemail)
        .filter(PhoneComVoicemail.call_id == call_id)
        .first()
    )
    customer_name = None
    if call.customer_id:
        customer = tenant_db.get(Customer, call.customer_id)
        customer_name = customer.name if customer else None
    job_title = None
    if call.job_id:
        job = tenant_db.get(Job, call.job_id)
        job_title = job.title if job else None

    rec_on = bool(_account_features(tenant_db).get("call-recording-on"))
    return {
        "id": str(call.id),
        "phone_com_call_id": call.phone_com_call_id,
        "direction": call.direction,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "duration_s": call.duration_s,
        "status": call.status,
        "extension_id": call.extension_id,
        "customer_id": str(call.customer_id) if call.customer_id else None,
        "customer_name": customer_name,
        "job_id": str(call.job_id) if call.job_id else None,
        "job_title": job_title,
        "has_recording": bool(call.recording_url) and rec_on,
        "has_voicemail": vm is not None and bool(vm.audio_url or vm.transcript),
    }


def _voicemail_for_call(tenant_db: Session, call_id: UUID) -> PhoneComVoicemail | None:
    return (
        tenant_db.query(PhoneComVoicemail)
        .filter(PhoneComVoicemail.call_id == call_id)
        .first()
    )


@router.get("/calls/{call_id}/recording")
def stream_call_recording(
    call_id: UUID,
    tenant_db: Session = Depends(get_tenant_db),
    control_db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    call = tenant_db.get(PhoneComCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    if not call.recording_url:
        raise HTTPException(status_code=404, detail="no recording available")

    tenant_id = _coerce_tenant_uuid(user)
    client = _get_phone_com_client(tenant_id, control_db, tenant_db)
    try:
        result = client.stream_call_recording({
            "call_recording_url": call.recording_url,
            "call_recording_cp_url": "",
        })
    except PhoneComAPIError as exc:
        raise HTTPException(status_code=502, detail="upstream stream failed") from exc

    if result is None:
        raise HTTPException(status_code=404, detail="no recording available")
    chunks, content_type = result
    return StreamingResponse(
        chunks,
        media_type=content_type or "audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/calls/{call_id}/voicemail-audio")
def stream_voicemail_audio(
    call_id: UUID,
    tenant_db: Session = Depends(get_tenant_db),
    control_db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    vm = _voicemail_for_call(tenant_db, call_id)
    if vm is None or not vm.audio_url:
        raise HTTPException(status_code=404, detail="no voicemail audio available")

    tenant_id = _coerce_tenant_uuid(user)
    client = _get_phone_com_client(tenant_id, control_db, tenant_db)
    try:
        chunks, content_type = client.stream_voicemail_audio({
            "voicemail_cp_url": vm.raw_payload.get("voicemail_cp_url", "") if vm.raw_payload else "",
            "voicemail_url": vm.audio_url,
        })
    except PhoneComAPIError as exc:
        raise HTTPException(status_code=502, detail="upstream stream failed") from exc
    return StreamingResponse(
        chunks,
        media_type=content_type or "audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/calls/{call_id}/voicemail-transcript")
def get_voicemail_transcript(
    call_id: UUID,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    vm = _voicemail_for_call(tenant_db, call_id)
    if vm is None:
        raise HTTPException(status_code=404, detail="no voicemail")
    return {
        "transcript": vm.transcript or "",
        "source": vm.transcript_source or "phone_com",
    }


# ── click-to-call (outbound origination) ───────────────────────────────


class OriginateCallIn(BaseModel):
    to: str = Field(..., min_length=3, max_length=40)
    customer_id: UUID | None = None
    job_id: UUID | None = None


def _caller_extension_for(tenant_db: Session, user_id: UUID | None) -> int:
    """Ring the logged-in tech's own Phone.com extension, falling back to the
    tenant default. Returns the numeric extension id or raises 503."""
    ext_raw: str | None = None
    if user_id is not None:
        row = (
            tenant_db.query(PhoneComExtension)
            .filter(PhoneComExtension.user_id == user_id)
            .first()
        )
        ext_raw = row.phone_com_extension_id if row else None
    if not ext_raw:
        app = tenant_db.query(AppSettings).first()
        ext_raw = app.phone_com_default_extension_id if app else None
    if not ext_raw:
        raise HTTPException(
            status_code=503,
            detail="No Phone.com extension for this user and no default set — Settings → Phone.com",
        )
    try:
        return int(ext_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="extension id is not numeric") from None


@router.post("/calls/originate")
def originate_call(
    payload: OriginateCallIn,
    tenant_db: Session = Depends(get_tenant_db),
    control_db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Click-to-call: Phone.com rings this tech's extension, then bridges the
    customer. The customer sees the tenant's default caller-id."""
    to_number = normalize_e164(payload.to) or payload.to
    tenant_id = _coerce_tenant_uuid(user)
    caller_extension = _caller_extension_for(tenant_db, _coerce_user_uuid(user))
    app = tenant_db.query(AppSettings).first()
    callee_caller_id = app.phone_com_default_caller_id if app else None
    client = _get_phone_com_client(tenant_id, control_db, tenant_db)
    try:
        result = client.originate_call(
            callee_phone_number=to_number,
            caller_extension=caller_extension,
            callee_caller_id=callee_caller_id,
        )
    except PhoneComAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ringing", "call": result}


# ── inbound MMS media proxy ────────────────────────────────────────────


def _attachment_url(att: Any) -> str | None:
    """Phone.com attachment shapes vary: a bare URL string, or a dict with a
    url/uri/media_url key. Return the first usable URL."""
    if isinstance(att, str):
        return att or None
    if isinstance(att, dict):
        for k in ("url", "uri", "media_url", "content_url"):
            v = att.get(k)
            if isinstance(v, str) and v:
                return v
    return None


@router.get("/messages/{message_id}/media/{idx}")
def stream_message_media(
    message_id: UUID,
    idx: int,
    tenant_db: Session = Depends(get_tenant_db),
    control_db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """Proxy-stream an inbound MMS attachment from Phone.com (auth'd, durable
    URLs expire), mirroring the voicemail-audio proxy — no local blob storage."""
    msg = tenant_db.get(PhoneComMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    attachments = msg.attachments or []
    if not (0 <= idx < len(attachments)):
        raise HTTPException(status_code=404, detail="attachment index out of range")
    url = _attachment_url(attachments[idx])
    if not url:
        raise HTTPException(status_code=404, detail="attachment has no url")

    tenant_id = _coerce_tenant_uuid(user)
    client = _get_phone_com_client(tenant_id, control_db, tenant_db)
    try:
        chunks, content_type = client.stream_url(url, requires_auth=True)
    except PhoneComAPIError as exc:
        raise HTTPException(status_code=502, detail="upstream media stream failed") from exc
    return StreamingResponse(
        chunks,
        media_type=content_type or "application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


def _mark_call_heard(tenant_db: Session, call_id: UUID, user_id: UUID | None) -> None:
    vm = _voicemail_for_call(tenant_db, call_id)
    if vm is None:
        # No voicemail row — still mark "heard" by stashing on raw_payload of the call
        call = tenant_db.get(PhoneComCall, call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        payload = dict(call.raw_payload or {})
        payload["heard_at"] = datetime.now(timezone.utc).isoformat()
        if user_id:
            payload["heard_by_user_id"] = str(user_id)
        call.raw_payload = payload
    else:
        vm.heard_at = datetime.now(timezone.utc)
        vm.heard_by_user_id = user_id
    tenant_db.commit()


@router.post("/calls/{call_id}/mark-heard", status_code=status.HTTP_204_NO_CONTENT)
def post_mark_call_heard(
    call_id: UUID,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    _mark_call_heard(tenant_db, call_id, _coerce_user_uuid(user))


# ── Wave G / S13: link a call to a job ─────────────────────────────────


class LinkJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID | None = None  # None unlinks


@router.patch("/calls/{call_id}/job")
def patch_call_job(
    call_id: UUID,
    payload: LinkJobIn,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    """Set or clear `phone_com_calls.job_id`. job_id=null unlinks."""
    call = tenant_db.get(PhoneComCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    if payload.job_id is not None:
        # Validate the job exists in this tenant before linking. The FK on
        # the column is ON DELETE SET NULL — we can write a stale ID and
        # the row would just appear unlinked, so verify here.
        job = tenant_db.get(Job, payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
    call.job_id = payload.job_id
    tenant_db.commit()
    return {"id": str(call.id), "job_id": str(call.job_id) if call.job_id else None}


# ── Wave G / S14: cold leads — grouped unmatched callers ───────────────


@router.get("/cold-leads")
def list_cold_leads(
    min_duration_s: int = Query(default=10, ge=0, le=3600),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    """Group calls with customer_id IS NULL by from_number. Filters out
    very short misdials (< min_duration_s). Returns one row per phone
    number with count, last_call_at, sample transcript, and the most
    recent call's status/cnam for quick scanning."""
    sub = (
        tenant_db.query(
            PhoneComCall.from_number.label("from_number"),
            func.count(PhoneComCall.id).label("call_count"),
            func.max(PhoneComCall.started_at).label("last_call_at"),
        )
        .filter(PhoneComCall.customer_id.is_(None))
        .filter(PhoneComCall.from_number.isnot(None))
        .filter(
            (PhoneComCall.duration_s.is_(None))
            | (PhoneComCall.duration_s >= min_duration_s),
        )
        .group_by(PhoneComCall.from_number)
        .order_by(desc("last_call_at"))
    )
    total = sub.count()
    rows = sub.offset((page - 1) * per_page).limit(per_page).all()

    items: list[dict[str, Any]] = []
    for r in rows:
        latest = (
            tenant_db.query(PhoneComCall)
            .filter(PhoneComCall.from_number == r.from_number)
            .filter(PhoneComCall.customer_id.is_(None))
            .order_by(desc(PhoneComCall.started_at))
            .first()
        )
        if latest is None:
            continue
        raw = latest.raw_payload or {}
        cnam = raw.get("caller_cnam") if isinstance(raw, dict) else None
        # Try to surface a voicemail transcript snippet for context.
        vm = (
            tenant_db.query(PhoneComVoicemail)
            .filter(PhoneComVoicemail.call_id == latest.id)
            .first()
        )
        snippet = (vm.transcript[:200] if vm and vm.transcript else None)
        items.append({
            "from_number": r.from_number,
            "caller_cnam": cnam,
            "call_count": int(r.call_count),
            "last_call_at": r.last_call_at.isoformat() if r.last_call_at else None,
            "last_status": latest.status,
            "last_call_id": str(latest.id),
            "voicemail_snippet": snippet,
        })
    return {"items": items, "total": total, "page": page, "per_page": per_page}


# ── pc-s10: messages ───────────────────────────────────────────────────


class MessageThreadOut(BaseModel):
    thread_key: str
    other_party_number: str | None
    customer_id: UUID | None
    customer_name: str | None
    last_message_at: datetime | None
    last_message_body: str | None
    last_message_direction: str | None
    message_count: int


class MessageOut(BaseModel):
    id: UUID
    direction: str
    from_number: str | None
    to_number: str | None
    body: str | None
    sent_at: datetime | None
    received_at: datetime | None
    delivery_status: str | None
    attachments: list[Any]
    customer_id: UUID | None
    job_id: UUID | None


@router.get("/messages/threads")
def list_message_threads(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    """Each row = one conversation. Last message wins for display fields."""
    # Aggregate: max(sent_at), count(*) per thread_key
    sub = (
        tenant_db.query(
            PhoneComMessage.thread_key,
            func.max(PhoneComMessage.sent_at).label("last_at"),
            func.count(PhoneComMessage.id).label("msg_count"),
        )
        .group_by(PhoneComMessage.thread_key)
        .order_by(desc("last_at"))
    )
    total = sub.count()
    rows = sub.offset((page - 1) * per_page).limit(per_page).all()

    items: list[dict[str, Any]] = []
    for row in rows:
        latest = (
            tenant_db.query(PhoneComMessage)
            .filter(PhoneComMessage.thread_key == row.thread_key)
            .order_by(desc(PhoneComMessage.sent_at))
            .first()
        )
        if latest is None:
            continue
        # The "other party" is whichever number is NOT the tenant's own DID.
        # Without a definitive list of own-numbers we use direction heuristic:
        # for inbound, other=from_number; for outbound, other=to_number.
        other = latest.from_number if latest.direction == "in" else latest.to_number
        customer_name = None
        if latest.customer_id:
            cust = tenant_db.get(Customer, latest.customer_id)
            customer_name = cust.name if cust else None
        items.append({
            "thread_key": row.thread_key,
            "other_party_number": other,
            "customer_id": str(latest.customer_id) if latest.customer_id else None,
            "customer_name": customer_name,
            "last_message_at": latest.sent_at.isoformat() if latest.sent_at else None,
            "last_message_body": latest.body,
            "last_message_direction": latest.direction,
            "message_count": int(row.msg_count),
        })
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/messages/threads/{thread_key}")
def get_message_thread(
    thread_key: str,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=500),
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    q = tenant_db.query(PhoneComMessage).filter(
        PhoneComMessage.thread_key == thread_key
    )
    total = q.count()
    rows = (
        q.order_by(PhoneComMessage.sent_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "thread_key": thread_key,
        "items": [
            {
                "id": str(r.id),
                "direction": r.direction,
                "from_number": r.from_number,
                "to_number": r.to_number,
                "body": r.body,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "delivery_status": r.delivery_status,
                "attachments": r.attachments or [],
                "customer_id": str(r.customer_id) if r.customer_id else None,
                "job_id": str(r.job_id) if r.job_id else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


class SendMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1, max_length=1600)
    customer_id: UUID | None = None
    job_id: UUID | None = None
    extension_id: int | None = None

    @field_validator("to")
    @classmethod
    def _normalize(cls, v: str) -> str:
        n = normalize_e164(v)
        if not n:
            raise ValueError("to: not a valid phone number")
        return n


def _thread_key_for(a: str, b: str) -> str:
    """Canonical pair: sorted lexicographically so direction doesn't matter."""
    return "|".join(sorted([a or "", b or ""]))


@router.post("/messages")
def send_message(
    payload: SendMessageIn,
    request: Request,  # noqa: ARG001
    tenant_db: Session = Depends(get_tenant_db),
    control_db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    tenant_id = _coerce_tenant_uuid(user)
    # F-55 / 2026-04-29 — resolver picks DID per tenant strategy:
    # conversation_sticky → tech_override → tenant_default.
    from gdx_dispatch.modules.phone_com.outbound_did import resolve_outbound_did
    from_number = resolve_outbound_did(
        tenant_db,
        customer_id=getattr(payload, "customer_id", None),
        to_number=payload.to,
        sending_user_id=_coerce_user_uuid(user),
    ) or ""
    if not from_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No outbound number configured. Set Settings → Phone.com → Default outbound number.",
        )

    client = _get_phone_com_client(tenant_id, control_db, tenant_db)
    try:
        result = client.send_message(
            from_number=from_number,
            to_number=payload.to,
            body=payload.body,
            extension_id=payload.extension_id,
        )
    except PhoneComAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    msg = PhoneComMessage(
        # Unique fallback when Phone.com's POST response omits an id — a
        # constant here would collide with the column's UNIQUE index on the
        # second id-less send (after the SMS already went out).
        phone_com_message_id=str(result.get("id") or f"out-{uuid4()}"),
        thread_key=_thread_key_for(from_number, payload.to),
        direction="out",
        from_number=from_number,
        to_number=payload.to,
        body=payload.body,
        sent_at=datetime.now(timezone.utc),
        delivery_status=result.get("status") or "queued",
        attachments=[],
        customer_id=payload.customer_id,
        job_id=payload.job_id,
        sent_by_user_id=_coerce_user_uuid(user),
        raw_payload=result,
    )
    tenant_db.add(msg)
    tenant_db.commit()
    tenant_db.refresh(msg)
    return {
        "id": str(msg.id),
        "phone_com_message_id": msg.phone_com_message_id,
        "thread_key": msg.thread_key,
        "delivery_status": msg.delivery_status,
    }


# ── pc-s11: stats + catalog ─────────────────────────────────────────────


@router.get("/stats/summary")
def get_stats_summary(
    days: int = Query(default=30, ge=1, le=365),
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    rows = (
        tenant_db.query(PhoneComStatsDaily)
        .order_by(desc(PhoneComStatsDaily.stat_date))
        .limit(days)
        .all()
    )
    if not rows:
        return {
            "days": days,
            "calls_in": 0,
            "calls_out": 0,
            "calls_missed": 0,
            "sms_in": 0,
            "sms_out": 0,
            "voicemails_new": 0,
            "total_call_minutes": 0,
            "by_day": [],
        }
    return {
        "days": days,
        "calls_in": sum(r.calls_in for r in rows),
        "calls_out": sum(r.calls_out for r in rows),
        "calls_missed": sum(r.calls_missed for r in rows),
        "sms_in": sum(r.sms_in for r in rows),
        "sms_out": sum(r.sms_out for r in rows),
        "voicemails_new": sum(r.voicemails_new for r in rows),
        "total_call_minutes": sum(r.total_call_minutes for r in rows),
        "by_day": [
            {
                "date": r.stat_date.isoformat(),
                "calls_in": r.calls_in,
                "calls_out": r.calls_out,
                "calls_missed": r.calls_missed,
                "sms_in": r.sms_in,
                "sms_out": r.sms_out,
                "voicemails_new": r.voicemails_new,
                "total_call_minutes": r.total_call_minutes,
            }
            for r in rows
        ],
    }


@router.get("/extensions")
def list_extensions(
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    rows = (
        tenant_db.query(PhoneComExtension)
        .order_by(PhoneComExtension.number.asc())
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "phone_com_extension_id": r.phone_com_extension_id,
                "name": r.name,
                "number": r.number,
                "user_id": str(r.user_id) if r.user_id else None,
                "is_active": r.is_active,
                "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/numbers")
def list_phone_numbers(
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    rows = (
        tenant_db.query(PhoneComNumber)
        .order_by(desc(PhoneComNumber.is_default_outbound), PhoneComNumber.phone_com_number.asc())
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "phone_com_number": r.phone_com_number,
                "label": r.label,
                "campaign_tag": r.campaign_tag,
                "is_default_outbound": r.is_default_outbound,
                "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


# F-55 / 2026-04-29 — manage marketing-attribution metadata + outbound DID
# strategy. Inbound attribution comes "for free" from PhoneComCall.to_number
# joined against this label; the new bit is being able to *name* what each
# DID is for ("Google Ads", "Yard Sign", etc.).

class _PhoneNumberMetaIn(BaseModel):
    label: str | None = None
    campaign_tag: str | None = None
    is_default_outbound: bool | None = None


@router.patch("/numbers/{number_id}")
def update_phone_number_meta(
    number_id: str,
    payload: _PhoneNumberMetaIn,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner", "manager"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    row = tenant_db.query(PhoneComNumber).filter(PhoneComNumber.id == number_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="number not found")
    if payload.label is not None:
        row.label = payload.label[:200]
    if payload.campaign_tag is not None:
        row.campaign_tag = payload.campaign_tag[:100] or None
    if payload.is_default_outbound is True:
        # Only one default — clear the others first.
        tenant_db.query(PhoneComNumber).update({PhoneComNumber.is_default_outbound: False})
        row.is_default_outbound = True
        # Mirror the legacy default-caller-id field so existing code paths
        # (and the outbound resolver's tenant-default fallback) line up.
        app = tenant_db.query(AppSettings).first()
        if app:
            app.phone_com_default_caller_id = row.phone_com_number
    elif payload.is_default_outbound is False:
        row.is_default_outbound = False
    tenant_db.commit()
    return {
        "id": str(row.id),
        "phone_com_number": row.phone_com_number,
        "label": row.label,
        "campaign_tag": row.campaign_tag,
        "is_default_outbound": row.is_default_outbound,
    }


class _StrategyIn(BaseModel):
    strategy: str
    default_caller_id: str | None = None


@router.get("/strategy")
def get_outbound_strategy(
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    from gdx_dispatch.modules.phone_com.outbound_did import candidate_strategies
    app = tenant_db.query(AppSettings).first()
    return {
        "strategy": (app.phone_com_outbound_strategy if app else "tenant_default") or "tenant_default",
        "default_caller_id": app.phone_com_default_caller_id if app else None,
        "candidates": list(candidate_strategies()),
    }


@router.patch("/strategy")
def set_outbound_strategy(
    payload: _StrategyIn,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    from gdx_dispatch.modules.phone_com.outbound_did import candidate_strategies
    if payload.strategy not in candidate_strategies():
        raise HTTPException(status_code=422, detail=f"invalid strategy '{payload.strategy}'")
    app = tenant_db.query(AppSettings).first()
    if not app:
        app = AppSettings()
        tenant_db.add(app)
    app.phone_com_outbound_strategy = payload.strategy
    if payload.default_caller_id is not None:
        app.phone_com_default_caller_id = payload.default_caller_id or None
    tenant_db.commit()
    return get_outbound_strategy(tenant_db, user)


# ── P2.9: mark-thread-read (Phone.com conversation sync) ────────────────


@router.post(
    "/messages/threads/{thread_key}/mark-read",
    status_code=status.HTTP_204_NO_CONTENT,
)
def mark_thread_read(
    thread_key: str,
    user: dict[str, Any] = Depends(get_current_user),
    control_db: Session = Depends(get_db),
    tenant_db: Session = Depends(get_tenant_db),
) -> None:
    """Mark every message in this thread read on Phone.com so the desk
    phone / mobile app stop showing the unread badge.

    Best-effort: if the messages don't carry a ``phone_com_conversation_id``
    yet (older rows from before P2.9), we 204 without calling upstream.
    The next webhook delivery will populate the id and a subsequent
    mark-read call will then sync.
    """
    tid = _coerce_tenant_uuid(user)
    rows = (
        tenant_db.query(PhoneComMessage)
        .filter(PhoneComMessage.thread_key == thread_key)
        .filter(PhoneComMessage.phone_com_conversation_id.isnot(None))
        .all()
    )
    if not rows:
        return None
    # The conversation_id is keyed per (extension, conversation). Group by
    # the upstream id and patch each unique one.
    conversation_ids = sorted({r.phone_com_conversation_id for r in rows if r.phone_com_conversation_id})
    if not conversation_ids:
        return None
    app = tenant_db.query(AppSettings).first()
    extension_id_raw = app.phone_com_default_extension_id if app else None
    if not extension_id_raw:
        # Without an extension we can't address the conversation; surface
        # 503 instead of silently no-op'ing — the operator must pick a
        # default extension before mark-read works.
        raise HTTPException(
            status_code=503,
            detail="Phone.com default_extension_id not set — Settings → Phone.com",
        )
    try:
        extension_id = int(extension_id_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="default_extension_id is not numeric") from None
    try:
        with _get_phone_com_client(tid, control_db, tenant_db) as c:
            for cid in conversation_ids:
                c.patch_conversation(
                    extension_id=extension_id,
                    conversation_id=cid,
                    read=True,
                )
    except PhoneComAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from exc
    return None


# ── P2.7: faxes (inbound list + PDF stream) ─────────────────────────────


@router.get("/faxes")
def list_faxes(
    direction: str | None = Query(default=None, pattern=r"^(in|out)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    """Local fax list — populated by the webhook receiver and the daily
    reconcile sync. No upstream call on the read path."""
    q = tenant_db.query(PhoneComFax)
    if direction is not None:
        q = q.filter(PhoneComFax.direction == direction)
    q = q.order_by(desc(PhoneComFax.received_at), desc(PhoneComFax.created_at))
    rows = q.limit(limit).offset(offset).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "phone_com_fax_id": r.phone_com_fax_id,
                "direction": r.direction,
                "from_number": r.from_number,
                "to_number": r.to_number,
                "pages": r.pages,
                "status": r.status,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "customer_id": str(r.customer_id) if r.customer_id else None,
                "heard_at": r.heard_at.isoformat() if r.heard_at else None,
            }
            for r in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/faxes/{fax_id}/pdf")
def stream_fax_pdf(
    fax_id: UUID,
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
    control_db: Session = Depends(get_db),
    tenant_db: Session = Depends(get_tenant_db),
) -> StreamingResponse:
    """Stream the PDF from Phone.com without exposing the upstream URL or
    Bearer token to the caller. Caller must be authenticated; module-gated
    by the router-level dep."""
    row = tenant_db.query(PhoneComFax).filter(PhoneComFax.id == fax_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="fax not found")
    tid = _coerce_tenant_uuid(user)
    try:
        pc_fax_id = int(row.phone_com_fax_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail="fax id is not an integer") from None
    try:
        with _get_phone_com_client(tid, control_db, tenant_db) as c:
            chunks, ctype = c.stream_fax_pdf(fax_id=pc_fax_id)
    except PhoneComAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from exc
    if not row.pdf_fetched_at:
        row.pdf_fetched_at = datetime.now(timezone.utc)
        tenant_db.commit()
    return StreamingResponse(chunks, media_type=ctype or "application/pdf")


# ── P2.6: blocked calls (call-side spam control) ────────────────────────


class _BlockedCallIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    number: str = Field(min_length=4, max_length=40)
    direction: str = Field(default="in", pattern=r"^(in|out)$")
    action: str = Field(default="block", pattern=r"^(block|voicemail)$")

    @field_validator("number")
    @classmethod
    def _e164(cls, v: str) -> str:
        digits = v.lstrip("+")
        if not v.startswith("+") or not digits.isdigit() or not (8 <= len(digits) <= 15):
            raise ValueError("number must look like +<8-15 digits>")
        return v


@router.get("/blocked-calls")
def list_blocked_calls(
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
    control_db: Session = Depends(get_db),
    tenant_db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    """Pass-through to Phone.com — blocked-calls are not local data, they
    live in the Phone.com account. We don't cache them since the list is
    short and changes rarely."""
    tid = _coerce_tenant_uuid(user)
    try:
        with _get_phone_com_client(tid, control_db, tenant_db) as c:
            return c.list_blocked_calls()
    except PhoneComAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from exc


@router.post("/blocked-calls", status_code=status.HTTP_201_CREATED)
def post_blocked_call(
    payload: _BlockedCallIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    control_db: Session = Depends(get_db),
    tenant_db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner", "manager"}:
        raise HTTPException(status_code=403, detail="admin/manager required")
    tid = _coerce_tenant_uuid(user)
    try:
        with _get_phone_com_client(tid, control_db, tenant_db) as c:
            out = c.create_blocked_call(
                name=payload.name, number=payload.number,
                direction=payload.direction, action=payload.action,
            )
    except PhoneComAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from exc
    from gdx_dispatch.core.audit import log_audit_event_sync
    try:
        log_audit_event_sync(
            control_db,
            tenant_id=str(tid),
            user_id=str(_coerce_user_uuid(user) or ""),
            action="phone_com.blocked_call.created",
            entity_type="phone_com_blocked_call",
            entity_id=str(out.get("id")),
            details={"number": payload.number, "direction": payload.direction,
                     "action": payload.action},
            request=request,
        )
        control_db.commit()
    except Exception:  # noqa: BLE001
        log.exception("blocked_call audit failed")
    return out


@router.delete("/blocked-calls/{blocked_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_blocked_call(
    blocked_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    control_db: Session = Depends(get_db),
    tenant_db: Session = Depends(get_tenant_db),
) -> None:
    if (user.get("role") or "").lower() not in {"admin", "owner", "manager"}:
        raise HTTPException(status_code=403, detail="admin/manager required")
    tid = _coerce_tenant_uuid(user)
    try:
        with _get_phone_com_client(tid, control_db, tenant_db) as c:
            c.delete_blocked_call(blocked_call_id=blocked_id)
    except PhoneComAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc),
        ) from exc
    from gdx_dispatch.core.audit import log_audit_event_sync
    try:
        log_audit_event_sync(
            control_db,
            tenant_id=str(tid),
            user_id=str(_coerce_user_uuid(user) or ""),
            action="phone_com.blocked_call.deleted",
            entity_type="phone_com_blocked_call",
            entity_id=str(blocked_id),
            details={},
            request=request,
        )
        control_db.commit()
    except Exception:  # noqa: BLE001
        log.exception("blocked_call audit failed")


@router.get("/inbound-stats")
def inbound_stats(
    days: int = 30,
    tenant_db: Session = Depends(get_tenant_db),
    user: dict[str, Any] = Depends(get_current_user),  # noqa: ARG001
) -> dict[str, Any]:
    """Calls + SMS by inbound DID + campaign_tag. Marketing attribution."""
    from datetime import datetime, timedelta, timezone as _tz
    since = datetime.now(_tz.utc) - timedelta(days=max(1, min(int(days), 365)))
    rows = tenant_db.execute(
        _text(
            "SELECT pcn.phone_com_number, pcn.label, pcn.campaign_tag, "
            "       COUNT(DISTINCT pcc.id) FILTER (WHERE pcc.direction = 'in') AS calls_in, "
            "       COUNT(DISTINCT pcm.id) FILTER (WHERE pcm.direction = 'in') AS sms_in "
            "FROM phone_com_numbers pcn "
            "LEFT JOIN phone_com_calls pcc "
            "       ON pcc.to_number = pcn.phone_com_number "
            "      AND pcc.start_at >= :since "
            "LEFT JOIN phone_com_messages pcm "
            "       ON pcm.to_number = pcn.phone_com_number "
            "      AND pcm.sent_at >= :since "
            "GROUP BY pcn.phone_com_number, pcn.label, pcn.campaign_tag "
            "ORDER BY (COUNT(DISTINCT pcc.id) + COUNT(DISTINCT pcm.id)) DESC"
        ),
        {"since": since},
    ).mappings().all()
    return {
        "since": since.isoformat(),
        "items": [dict(r) for r in rows],
    }
