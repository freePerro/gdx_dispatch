"""Send a text later — a reply, an invoice link or an estimate link.

Phone.com v4's Send Message takes no send time and has no cancel
(apidocs.phone.com, read 2026-09-30), so a text chosen for "send later" waits in ``scheduled_sms`` until the
``phone_com.send_due_scheduled_sms`` beat task (every minute) sends it.

What this module guarantees, because an SMS cannot be recalled:

1. **One send per row.** The drain claims a row with a conditional UPDATE
   (``scheduled`` → ``sending``) and commits before calling anything; cancel is
   the same conditional UPDATE from the other side. Two drains, or a drain and
   a cancel, cannot both win.
2. **Never resent.** A definite failure is ``failed``, an ambiguous one
   (timeout, 5xx, a worker killed mid-send) is ``unknown``; neither is retried.
   The operator decides, after checking the thread.
3. **Judged at send time, not at schedule time.** Texting (the phone_com
   module) must still be on; the invoice / estimate adapters re-make every
   refusal (paid, void, accepted, opted out); the scheduler must still be an
   active user holding what the send-now route needs — ``invoices.send`` /
   ``estimates.send`` for the office, the job still assigned to them for a
   tech; and the default wording is rebuilt from the document as it is then —
   ``body`` is stored only when the operator edited it, because the invoice
   text quotes the amount due.
4. **On time or not at all.** A text more than ``LATE_LIMIT`` past its time
   (the worker or beat was down) is ``skipped`` as ``too_late``, never sent
   late: "send at 8 AM" must not become "send at 2 AM when the worker came
   back", and a reply saying "we'll be there at 9" must not land at 3 PM.
5. **Every outcome audited** (``sms_scheduled``, ``sms_schedule_canceled``,
   ``scheduled_sms_sent`` / ``_skipped`` / ``_failed`` / ``_unknown``), on top
   of the adapters' own rows — and a text that did not go puts an alert in
   the office bell, because nobody is watching the thread at 8 AM.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.link_sms import adopt_message_id, as_uuid, definitely_not_sent, refuse
from gdx_dispatch.modules.phone_com.models import PhoneComMessage, ScheduledSms

log = logging.getLogger(__name__)

MIN_LEAD = timedelta(minutes=1)
MAX_LEAD = timedelta(days=30)
# A row still "sending" after this long lost its worker mid-send (celery-low
# and -high are recreated on every deploy): it becomes "unknown", never resent.
STALE_SENDING = timedelta(minutes=10)
BATCH_SIZE = 25
# Past this, a due text is held back rather than sent late (see 4 above).
LATE_LIMIT = timedelta(minutes=30)
# One drain stops claiming new rows after this long, so a slow Phone.com
# cannot hold a priority:high worker slot for a whole batch (25 × a 45-second
# timeout); what is left is picked up by the next minute's drain.
DRAIN_BUDGET = timedelta(seconds=50)
# Outcomes worth showing beside the thread / in the dialog after the fact:
# the operator must learn that a text they scheduled did not go.
RECENT_OUTCOME_WINDOW = timedelta(days=7)

KIND_MESSAGE = "message"
KIND_INVOICE = "invoice"
KIND_ESTIMATE = "estimate"

ACTION_MESSAGE = "sms_sent_scheduled"
ACTION_INVOICE = "invoice_sent_sms_scheduled"
ACTION_ESTIMATE = "estimate_sent_sms_scheduled"
ACTION_TECH_INVOICE = "mobile_invoice_sent_sms_scheduled"
ACTION_TECH_ESTIMATE = "mobile_estimate_sent_sms_scheduled"

# Office texts need at send time the permission their send-now route needs.
_OFFICE_PERMISSION = {ACTION_INVOICE: "invoices.send", ACTION_ESTIMATE: "estimates.send"}
# Tech texts are authorized by job ownership, as their send-now routes are.
_TECH_ACTIONS = {ACTION_TECH_INVOICE, ACTION_TECH_ESTIMATE}

_PENDING = ("scheduled", "sending")
_NOT_SENT = ("failed", "unknown", "skipped")


class ScheduleLinkSmsIn(BaseModel):
    """Composer payload for every /schedule-sms route (office and tech,
    invoices and estimates)."""

    model_config = ConfigDict(extra="forbid")
    to: str | None = Field(default=None, max_length=40)
    body: str | None = Field(default=None, max_length=1600)
    send_at: datetime


class ScheduleMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., min_length=1, max_length=40)
    body: str = Field(..., min_length=1, max_length=1600)
    customer_id: UUID | None = None
    job_id: UUID | None = None
    send_at: datetime


# ── helpers ──────────────────────────────────────────────────────────────


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every value here is stored UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    v = _utc(value)
    return v.isoformat().replace("+00:00", "Z") if v else None


def normalize_send_at(send_at: datetime, *, now: datetime | None = None) -> datetime:
    """The send time as UTC, or a {code, message} 422. Stored UTC because
    SQLite drops the offset — a local-offset value would read back hours off."""
    if send_at.tzinfo is None:
        raise refuse(422, "send_at_no_timezone", "The send time must include a timezone.")
    now = now or datetime.now(UTC)
    at = send_at.astimezone(UTC)
    if at < now + MIN_LEAD:
        raise refuse(422, "send_at_too_soon", "Pick a time at least a minute from now — or send it now.")
    if at > now + MAX_LEAD:
        raise refuse(422, "send_at_too_far", "Texts can be scheduled up to 30 days ahead.")
    return at


def _user_names(db: Session, ids: set[UUID]) -> dict[UUID, str]:
    from gdx_dispatch.models.tenant_models import User

    if not ids:
        return {}
    rows = db.execute(select(User.id, User.full_name, User.name, User.username).where(User.id.in_(ids))).all()
    return {r.id: (r.full_name or r.name or r.username or "") for r in rows}


def serialize(row: ScheduledSms, names: dict[UUID, str] | None = None) -> dict[str, Any]:
    names = names or {}
    return {
        "id": str(row.id),
        "kind": row.kind,
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "to_number": row.to_number,
        # None = the default wording, rebuilt from the document when it sends.
        "body": row.body,
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "job_id": str(row.job_id) if row.job_id else None,
        "send_at": _iso(row.send_at),
        "status": row.status,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "sent_at": _iso(row.sent_at),
        "canceled_at": _iso(row.canceled_at),
        "created_at": _iso(row.created_at),
        "created_by_user_id": str(row.created_by_user_id),
        "created_by_name": names.get(row.created_by_user_id) or None,
    }


def _audit(db: Session, row: ScheduledSms, action: str, *, user_id: Any, request: Any = None, **extra: Any) -> None:
    from gdx_dispatch.core.audit import audit_best_effort

    audit_best_effort(
        db,
        action=action,
        entity_type="scheduled_sms",
        entity_id=str(row.id),
        tenant_id=row.tenant_id,
        user_id=str(user_id) if user_id else None,
        request=request,
        details={
            "kind": row.kind,
            "document_id": str(row.entity_id) if row.entity_id else None,
            "to_last4": (row.to_number or "")[-4:],
            "send_at": _iso(row.send_at),
            "status": row.status,
            "error_code": row.error_code,
            **extra,
        },
    )


# ── schedule / list / cancel ────────────────────────────────────────────


def create(
    db: Session,
    *,
    kind: str,
    to: str,
    body: str | None,
    send_at: datetime,
    audit_action: str,
    tenant_id: UUID | str,
    user_id: Any,
    entity_id: UUID | None = None,
    customer_id: UUID | None = None,
    job_id: UUID | None = None,
    request: Any = None,
) -> dict[str, Any]:
    from gdx_dispatch.core.audit import audit_or_rollback

    actor = as_uuid(user_id)
    if actor is None:
        # A scheduled text sends as a person; with no person there is no one
        # to re-check at send time or to answer for it.
        raise refuse(403, "no_user", "Only a signed-in user can schedule a text.")
    row = ScheduledSms(
        id=uuid4(),
        kind=kind,
        entity_id=entity_id,
        to_number=to,
        body=body,
        customer_id=customer_id,
        job_id=job_id,
        send_at=normalize_send_at(send_at),
        status="scheduled",
        audit_action=audit_action,
        tenant_id=str(tenant_id),
        created_by_user_id=actor,
    )
    db.add(row)
    db.flush()
    audit_or_rollback(
        db,
        action="sms_scheduled",
        entity_type="scheduled_sms",
        entity_id=str(row.id),
        tenant_id=str(tenant_id),
        request=request,
        actor={"user_id": str(actor)},
        details={
            "kind": kind,
            "document_id": str(entity_id) if entity_id else None,
            "to_last4": to[-4:],
            "send_at": _iso(row.send_at),
            "edited_body": body is not None,
        },
    )
    db.commit()
    db.refresh(row)
    return serialize(row, _user_names(db, {row.created_by_user_id}))


def schedule_link(
    db: Session,
    doc: Any,
    *,
    kind: str,
    prepare: Any,
    payload: ScheduleLinkSmsIn,
    audit_action: str,
    tenant_id: UUID,
    user_id: Any,
    request: Any = None,
) -> dict[str, Any]:
    """Schedule an invoice / estimate link text. ``prepare`` is the adapter's
    (db, doc, to_override=) → preview; its refusal is raised NOW so the
    operator learns tonight, not tomorrow, that it cannot go."""
    prep = prepare(db, doc, to_override=payload.to)
    if prep["blocked"]:
        b = prep["blocked"]
        raise refuse(409 if b["code"] != "no_valid_phone" else 422, b["code"], b["message"])
    edited = (payload.body or "").strip()
    default = (prep.get("body") or "").strip()
    return create(
        db,
        kind=kind,
        to=prep["to"],
        # The unedited default is rebuilt at send time — an invoice paid down
        # overnight must not be texted with last night's balance.
        body=edited if edited and edited != default else None,
        send_at=payload.send_at,
        audit_action=audit_action,
        tenant_id=tenant_id,
        user_id=user_id,
        entity_id=doc.id,
        customer_id=getattr(doc, "customer_id", None),
        job_id=getattr(doc, "job_id", None),
        request=request,
    )


def list_rows(
    db: Session,
    *,
    to: str | None = None,
    kind: str | None = None,
    entity_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Waiting texts, plus the last week's ones that did NOT go — for one
    number (the SMS thread) or one document (the text dialog)."""
    q = select(ScheduledSms)
    if to:
        q = q.where(ScheduledSms.to_number == to)
    if kind:
        q = q.where(ScheduledSms.kind == kind)
    if entity_id:
        q = q.where(ScheduledSms.entity_id == entity_id)
    cutoff = datetime.now(UTC) - RECENT_OUTCOME_WINDOW
    q = q.where(
        ScheduledSms.status.in_(_PENDING)
        | (ScheduledSms.status.in_(_NOT_SENT) & (ScheduledSms.send_at >= cutoff))
    ).order_by(ScheduledSms.send_at)
    rows = db.execute(q).scalars().all()
    names = _user_names(db, {r.created_by_user_id for r in rows})
    return [serialize(r, names) for r in rows]


def cancel(db: Session, row_id: UUID, *, user_id: Any, request: Any = None) -> dict[str, Any]:
    row = db.get(ScheduledSms, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scheduled text not found")
    now = datetime.now(UTC)
    won = db.execute(
        update(ScheduledSms)
        .where(ScheduledSms.id == row_id, ScheduledSms.status == "scheduled")
        .values(status="canceled", canceled_at=now, canceled_by_user_id=as_uuid(user_id), updated_at=now)
    ).rowcount
    db.commit()
    db.refresh(row)
    if won != 1:
        raise refuse(
            409, "not_cancelable",
            "Too late to cancel — this text is already sending or done." if row.status != "canceled"
            else "This text was already canceled.",
        )
    _audit(db, row, "sms_schedule_canceled", user_id=user_id, request=request)
    return serialize(row, _user_names(db, {row.created_by_user_id}))


# ── the drain ────────────────────────────────────────────────────────────


class _WorkerRequest:
    """What the permission resolver reads off a request: only the tenant id."""

    def __init__(self, tenant_id: str) -> None:
        self.state = type("S", (), {"tenant": {"id": tenant_id}})()
        self.headers: dict = {}
        self.client = None


def _fire_refusal(db: Session, row: ScheduledSms) -> tuple[str, str] | None:
    """Everything the send-now route would check about who is sending and
    whether texting is on — re-made when the text fires."""
    from gdx_dispatch.core import modules as core_modules
    from gdx_dispatch.core.job_access import job_belongs_to_user
    from gdx_dispatch.core.modules import _load_user_permissions
    from gdx_dispatch.core.permissions import WILDCARD
    from gdx_dispatch.models.tenant_models import User

    # The office's kill switch: every send-now route is gated on the module.
    if not core_modules.is_module_enabled("phone_com", _WorkerRequest(row.tenant_id), db):
        return "texting_disabled", "Texting (Phone.com) was turned off before this text was due."

    user = db.execute(
        select(User).where(User.id == row.created_by_user_id, User.deleted_at.is_(None))
    ).scalar_one_or_none()
    if user is None or user.active is False:
        return "scheduler_not_permitted", "The person who scheduled this text no longer has an active account."
    perm = _OFFICE_PERMISSION.get(row.audit_action)
    if perm:
        perms = _load_user_permissions(
            db, _WorkerRequest(row.tenant_id), {"user_id": str(user.id), "role": user.role or ""}
        )
        if WILDCARD not in perms and perm not in perms:
            return "scheduler_not_permitted", f"The person who scheduled this text no longer holds {perm}."
    if row.audit_action in _TECH_ACTIONS and not (
        row.job_id and job_belongs_to_user(db, row.tenant_id, str(row.job_id), str(user.id))
    ):
        return "job_not_yours", "The job is no longer assigned to the tech who scheduled this text."
    return None


def _finish(db: Session, row: ScheduledSms, status: str, *, code: str | None = None,
            message: str | None = None, message_row_id: UUID | None = None) -> None:
    row.status = status
    row.error_code = code
    row.error_message = (message or None) and message[:1000]
    if status == "sent":
        row.sent_at = datetime.now(UTC)
    if message_row_id is not None:
        row.phone_com_message_row_id = message_row_id
    db.commit()
    action = {
        "sent": "scheduled_sms_sent",
        "skipped": "scheduled_sms_skipped",
        "failed": "scheduled_sms_failed",
        "unknown": "scheduled_sms_unknown",
    }[status]
    _audit(db, row, action, user_id=row.created_by_user_id)
    if status in _NOT_SENT:
        _alert_office(db, row)


_KIND_NOUN = {KIND_MESSAGE: "reply", KIND_INVOICE: "invoice text", KIND_ESTIMATE: "estimate text"}
# The bell's deep link (NotificationsDrawer.vue). Bell rows are broadcast to
# every user. An invoice / estimate alert opens billing / estimates, which a
# tech can open on the phone. A reply alert opens the SMS page, which needs
# nav.office — a tech who taps one lands on access-denied, as with the other
# office broadcasts (leads, payments); only the office can schedule a reply.
_KIND_CATEGORY = {KIND_MESSAGE: "sms", KIND_INVOICE: "invoice", KIND_ESTIMATE: "estimate"}


def _alert_office(db: Session, row: ScheduledSms) -> None:
    """The office bell (best-effort, never raises): the operator scheduled it
    and walked away, so a text that did not go must come and find them."""
    from gdx_dispatch.core.link_sms import active_customer
    from gdx_dispatch.core.office_notifications import notify_office

    try:
        customer = active_customer(db, row.customer_id) if row.customer_id else None
        who = (getattr(customer, "name", None) or "").strip() or f"…{(row.to_number or '')[-4:]}"
        noun = _KIND_NOUN.get(row.kind, "text")
        number = _document_number(db, row)
        if number:
            noun = f"{noun} for {number}"
        title = "Scheduled text not confirmed" if row.status == "unknown" else "Scheduled text not sent"
        notify_office(
            db, row.tenant_id,
            title=title,
            message=f"The scheduled {noun} to {who}: {row.error_message or row.error_code or row.status}",
            category=_KIND_CATEGORY.get(row.kind, "sms"),
        )
    except Exception:  # noqa: BLE001 — an alert must never fail the drain
        db.rollback()
        log.exception("scheduled_sms_alert_failed id=%s", row.id)


def _document_number(db: Session, row: ScheduledSms) -> str | None:
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    if row.kind == KIND_INVOICE and row.entity_id:
        return db.execute(select(Invoice.invoice_number).where(Invoice.id == row.entity_id)).scalar_one_or_none()
    if row.kind == KIND_ESTIMATE and row.entity_id:
        return db.execute(select(Estimate.estimate_number).where(Estimate.id == row.entity_id)).scalar_one_or_none()
    return None


def _outcome_of(exc: HTTPException) -> tuple[str, str, str]:
    """(status, code, message) for an adapter's exception. The adapters raise
    the same type for a refusal (nothing sent) and for a Phone.com outcome
    (maybe sent), so the code decides — never the type."""
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": None, "message": str(exc.detail)}
    code = detail.get("code") or f"http_{exc.status_code}"
    message = detail.get("message") or str(exc.detail)
    if code == "sms_outcome_unknown":
        return "unknown", code, message
    if code == "sent_but_not_recorded":
        return "sent", code, message
    if code == "sms_provider_error" or exc.status_code == 503:
        # A 503 is "Phone.com is not configured" — raised before any call.
        return "failed", code, message
    return "skipped", code, message


def _message_row_id(db: Session, phone_com_message_id: str | None) -> UUID | None:
    if not phone_com_message_id:
        return None
    return db.execute(
        select(PhoneComMessage.id).where(PhoneComMessage.phone_com_message_id == str(phone_com_message_id))
    ).scalar_one_or_none()


def _send_link_row(db: Session, row: ScheduledSms) -> None:
    from gdx_dispatch.core import estimate_sms, invoice_sms
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.modules.proposals.models import Estimate

    model, adapter, noun = (
        (Invoice, invoice_sms, "invoice") if row.kind == KIND_INVOICE else (Estimate, estimate_sms, "estimate")
    )
    doc = db.execute(
        select(model).where(model.id == row.entity_id, model.deleted_at.is_(None))
    ).scalar_one_or_none()
    if doc is None:
        _finish(db, row, "skipped", code=f"{noun}_not_found", message=f"The {noun} was deleted.")
        return
    if row.audit_action == ACTION_TECH_INVOICE and doc.verified_at is None:
        # The tech route's own rule, re-made: nothing a tech wrote reaches a
        # customer before the office has verified it.
        from gdx_dispatch.routers.mobile_invoicing import _AWAITING_VERIFICATION

        _finish(db, row, "skipped", code="awaiting_verification", message=_AWAITING_VERIFICATION)
        return
    try:
        result = adapter.send(
            db,
            doc,
            tenant_id=UUID(row.tenant_id),
            actor_id=str(row.created_by_user_id),
            to_override=row.to_number,
            body_override=row.body,
            audit_action=row.audit_action,
            request=None,
        )
    except HTTPException as exc:
        db.rollback()
        status, code, message = _outcome_of(exc)
        _finish(db, row, status, code=code, message=message)
        return
    _finish(db, row, "sent", message_row_id=_message_row_id(db, result.get("phone_com_message_id")))


def _send_message_row(db: Session, row: ScheduledSms) -> None:
    """A plain reply, with the link engine's safety rules: the attempt is a
    committed row before Phone.com is called, and an ambiguous outcome is
    "unknown", never retried."""
    import httpx

    from gdx_dispatch.core.link_sms import active_customer
    from gdx_dispatch.modules.phone_com.client import PhoneComAPIError
    from gdx_dispatch.modules.phone_com.outbound_did import resolve_outbound_did
    from gdx_dispatch.modules.phone_com.router import _get_phone_com_client, _thread_key_for

    if row.customer_id:
        customer = active_customer(db, row.customer_id)
        if customer is not None and customer.sms_opt_out:
            _finish(db, row, "skipped", code="sms_opt_out", message="This customer has opted out of text messages.")
            return
    from_number = resolve_outbound_did(
        db, customer_id=row.customer_id, to_number=row.to_number, sending_user_id=row.created_by_user_id
    )
    if not from_number:
        _finish(db, row, "failed", code="no_outbound_number",
                message="No outbound texting number is configured (Settings → Phone.com → Default outbound number).")
        return
    try:
        client = _get_phone_com_client(UUID(row.tenant_id), db, db)
    except HTTPException as exc:
        _finish(db, row, "failed", code="phone_com_not_configured", message=str(exc.detail))
        return
    body = row.body or ""
    if len(body) > client._SMS_MAX_BODY:
        _finish(db, row, "failed", code="body_too_long",
                message=f"Message is {len(body)} characters; the limit is {client._SMS_MAX_BODY}.")
        return

    msg = PhoneComMessage(
        id=uuid4(),
        phone_com_message_id=f"pending-{uuid4()}",
        thread_key=_thread_key_for(from_number, row.to_number),
        direction="out",
        from_number=from_number,
        to_number=row.to_number,
        body=body,
        sent_at=datetime.now(UTC),
        delivery_status="sending",
        attachments=[],
        customer_id=row.customer_id,
        job_id=row.job_id,
        sent_by_user_id=row.created_by_user_id,
        raw_payload={"scheduled_sms_id": str(row.id)},
    )
    db.add(msg)
    row.phone_com_message_row_id = msg.id
    db.commit()

    try:
        result = client.send_message(from_number=from_number, to_number=row.to_number, body=body)
    except (ValueError, TypeError):
        # Every non-2xx is PhoneComAPIError, so this is a 2xx whose body would
        # not parse: Phone.com accepted the text.
        result = {}
    except (PhoneComAPIError, httpx.HTTPError) as exc:
        db.rollback()
        definite = definitely_not_sent(exc)
        msg.delivery_status = "failed" if definite else "unknown"
        msg.delivery_failed_reason = str(exc)[:500]
        db.commit()
        if definite:
            _finish(db, row, "failed", code="sms_provider_error", message=f"Phone.com refused the text: {exc}")
        else:
            _finish(db, row, "unknown", code="sms_outcome_unknown",
                    message="Phone.com did not confirm the text — check the thread before sending again.")
        return

    if not isinstance(result, dict):
        result = {}
    adopt_message_id(db, msg, str(result.get("id") or msg.phone_com_message_id))
    msg.delivery_status = result.get("status") or "queued"
    msg.raw_payload = {**result, "scheduled_sms_id": str(row.id)}
    _finish(db, row, "sent", message_row_id=msg.id)


def _how_long(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} minutes"
    hours = minutes // 60
    return f"{hours} hours" if hours < 48 else f"{hours // 24} days"


def _fire(db: Session, row: ScheduledSms) -> None:
    late = datetime.now(UTC) - _utc(row.send_at)
    if late > LATE_LIMIT:
        _finish(db, row, "skipped", code="too_late",
                message=f"Not sent — it was due {_how_long(late)} earlier and texting was down or "
                        "behind at the time. Send it now or schedule it again.")
        return
    refusal = _fire_refusal(db, row)
    if refusal:
        _finish(db, row, "skipped", code=refusal[0], message=refusal[1])
        return
    if row.kind == KIND_MESSAGE:
        _send_message_row(db, row)
    elif row.kind in (KIND_INVOICE, KIND_ESTIMATE):
        _send_link_row(db, row)
    else:
        _finish(db, row, "skipped", code="unknown_kind", message=f"Unknown scheduled text kind {row.kind!r}.")


def reap_stale(db: Session, *, now: datetime | None = None) -> int:
    """Rows a killed worker left "sending": the text may or may not have gone,
    so they become "unknown" (never resent) and the operator is told to check."""
    now = now or datetime.now(UTC)
    stale = db.execute(
        select(ScheduledSms).where(
            ScheduledSms.status == "sending", ScheduledSms.attempted_at < now - STALE_SENDING
        )
    ).scalars().all()
    for row in stale:
        won = db.execute(
            update(ScheduledSms)
            .where(ScheduledSms.id == row.id, ScheduledSms.status == "sending")
            .values(status="unknown", error_code="worker_interrupted",
                    error_message="The send was interrupted — check the thread before sending again.",
                    updated_at=now)
        ).rowcount
        db.commit()
        if won == 1:
            db.refresh(row)
            _audit(db, row, "scheduled_sms_unknown", user_id=row.created_by_user_id)
            # Not through _finish, so the bell is raised here: a deploy that
            # restarts the worker mid-send is the commonest "unknown" of all.
            _alert_office(db, row)
    return len(stale)


def claim(db: Session, row_id: UUID, *, now: datetime | None = None) -> bool:
    """Take the row for sending — True only for the one caller that moved it
    from ``scheduled``. Committed before anything is sent, so a cancel or a
    second drain that read the same due list loses cleanly.

    ``attempted_at`` is the moment of the claim, not the drain's start: the
    stale-row reaper measures from it, and a row claimed late in a slow batch
    must not look ten minutes old while it is being sent."""
    now = now or datetime.now(UTC)
    won = db.execute(
        update(ScheduledSms)
        .where(ScheduledSms.id == row_id, ScheduledSms.status == "scheduled")
        .values(status="sending", attempted_at=now, updated_at=now)
    ).rowcount
    db.commit()
    return won == 1


def drain(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Send every due text once. One row's failure never blocks the batch."""
    now = now or datetime.now(UTC)
    counts = {"reaped": reap_stale(db, now=now), "claimed": 0, "sent": 0, "not_sent": 0}
    due = db.execute(
        select(ScheduledSms.id)
        .where(ScheduledSms.status == "scheduled", ScheduledSms.send_at <= now)
        .order_by(ScheduledSms.send_at)
        .limit(BATCH_SIZE)
    ).scalars().all()
    started = datetime.now(UTC)
    for row_id in due:
        if datetime.now(UTC) - started > DRAIN_BUDGET:
            break  # the rest stay "scheduled" for the next minute's drain
        if not claim(db, row_id):
            continue  # canceled, or another drain took it
        counts["claimed"] += 1
        row = db.get(ScheduledSms, row_id)
        db.refresh(row)
        try:
            _fire(db, row)
        except Exception as exc:  # noqa: BLE001 — one row must not stop the batch
            # Raised somewhere between the claim and the outcome: it may have
            # been after the text left, so "unknown", never "skipped".
            log.exception("scheduled_sms_fire_failed id=%s", row_id)
            db.rollback()
            db.refresh(row)
            if row.status == "sending":
                _finish(db, row, "unknown", code="send_error",
                        message=f"Sending failed unexpectedly ({type(exc).__name__}) — check the thread before sending again.")
        db.refresh(row)
        counts["sent" if row.status == "sent" else "not_sent"] += 1
    return counts
