"""Deliver queued win-back sends (GDXA-1723).

``POST /api/winback/campaigns/{id}/send`` writes one ``winback_sends`` row per
customer as ``queued`` and moves the campaign to ``sending``. Until this task
existed nothing read those rows, so a campaign the screen called "sent" had
reached nobody (GDXA-1721). This beat task, every minute, is the reader.

What it guarantees, because a text or an email cannot be recalled:

1. **One send per row.** A row is claimed by a conditional UPDATE
   (``queued`` → ``sending``) committed before any transport is called, so two
   overlapping drains cannot both win it. Portable compare-and-set rather than
   ``FOR UPDATE SKIP LOCKED``: SQLite has no row locks, and the guard is the
   same on both dialects.
2. **Never resent.** A definite failure is ``failed``; an SMS whose outcome is
   ambiguous (read timeout, 5xx, no answer within ``WORST_SEND``), or a row a
   killed worker left ``sending``, is ``unknown``. None is retried. The one
   row put back to ``queued`` is a text Phone.com provably never got (a 429
   or no connection): it counts as ``not_reached``, stops this pass's texts,
   and is ``failed`` after ``NOT_REACHED_TRIES`` passes.
3. **Judged at send time.** The customer must still exist, must not have
   opted out of that channel (``email_opt_out`` / ``sms_opt_out`` — win-back is
   marketing), and texting must still be switched on. A refusal is
   ``skipped`` with the reason in ``error``.
4. **Only campaigns in ``sending``.** Rows under any other campaign status are
   never touched, so a campaign written before this task existed (which the
   router marked ``sent`` at enqueue) is not delivered months late.
5. **The campaign settles.** When none of its rows is ``queued`` or
   ``sending``, the campaign becomes ``sent`` (at least one delivered or
   possibly delivered) or ``failed`` (provably none), with ``sent_at``, and one ``winback_campaign_sent`` /
   ``winback_campaign_failed`` audit row carrying the counts. Each drain pass
   that touched rows writes one ``winback_sends_drained`` audit row per
   campaign. The person who pressed Send is on ``winback_campaign_queued``.

``sent_at`` doubles as the claim time while a row is ``sending`` (no migration
for a ``claimed_at``): the stale-row reaper measures from it. It is rewritten
to the delivery time on ``sent`` and cleared on every other outcome, so after
settling it means what it says.
"""
from __future__ import annotations

import contextlib
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal, contained_read
from gdx_dispatch.models.tenant_models import AppSettings, Customer, WinbackCampaign, WinbackSend

log = logging.getLogger(__name__)

BATCH_SIZE = 25
# The priority:low worker is one slot on a customer install (docker-compose.yml's
# GDX_CELERY_LOW_CONCURRENCY default of 1)
# and the every-minute inbound SMS poll queues behind this drain there (it
# expires after 50s), so a pass must hand the slot back in time. A row is
# claimed only while the budget left still covers one whole text
# (WORST_SEND), and a text is never waited on past WORST_SEND, so the texting
# loop ends within DRAIN_BUDGET of its start. What is left goes to the next
# pass; claims are per row, so an overlap is safe anyway.
# Not bounded by this: an email. Its transport (Graph with its own timeout and
# 429 retries, then SMTP at 15s) takes no deadline from the drain, so a slow
# mailbox can still run a pass past the budget.
DRAIN_BUDGET = timedelta(seconds=30)
# The wall-clock cap on one Phone.com send. httpx's timeouts are per
# operation (a response that dribbles in resets the read timeout on every
# chunk), so the send runs on a thread the drain stops waiting for at
# WORST_SEND; the outcome is then "unknown", never retried. SEND_TIMEOUT
# makes httpx give up first in the ordinary slow case.
WORST_SEND = timedelta(seconds=15)
SEND_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=1.0)
# _send_sms returns this when Phone.com provably never got the text (429, no
# connection): the row goes back to "queued" and this pass sends no more
# texts (emails still go). After NOT_REACHED_TRIES passes it is "failed", so
# an outage cannot hold a campaign open, or fill the customer's thread with
# failed copies, forever. The try count rides in ``error``; no migration.
NOT_REACHED = "queued"
NOT_REACHED_TRIES = 5
_TRY_RE = re.compile(r"^sms_not_reached \(try (\d+) of ")


def _not_reached_tries(error: str | None) -> int:
    m = _TRY_RE.match(error or "")
    return int(m.group(1)) if m else 0
# A row still "sending" after this long lost its worker mid-send (celery
# workers are recreated on every deploy): it may have gone, so "unknown".
STALE_SENDING = timedelta(minutes=15)
DRAINER = "system:winback_drain"
OPEN_STATUSES = ("queued", "sending")


class _WorkerRequest:
    """The module-grant check reads only the tenant id off a request."""

    def __init__(self, tenant_id: str) -> None:
        self.state = type("S", (), {"tenant": {"id": tenant_id}})()
        self.headers: dict = {}
        self.client = None


def render_body(template: str, customer: Any) -> str:
    """Fill the placeholders. ``str.replace``, not ``format``: a stray brace in
    an operator's template must not raise or leak attributes."""
    name = (getattr(customer, "name", "") or "").strip()
    first = name.split()[0] if name else ""
    out = template or ""
    for token, value in (("{customer_name}", name), ("{first_name}", first), ("{name}", name)):
        out = out.replace(token, value)
    return out


def _audit(db: Session, tenant_id: str, action: str, campaign_id: UUID, details: dict[str, Any]) -> None:
    """Best-effort: the messages have already gone, so a failed trail row is
    logged, never raised into the drain (which would strand claimed rows)."""
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=DRAINER,
            action=action,
            entity_type="winback_campaign",
            entity_id=str(campaign_id),
            details=details,
        )
        db.commit()
    except Exception:
        log.exception("winback_drain_audit_failed action=%s campaign=%s", action, campaign_id)
        with contextlib.suppress(Exception):
            db.rollback()


def _automation_sender(db: Session) -> str | None:
    """The tenant's automation mailbox, as workflow rules and plugin mail use;
    None falls back to SMTP inside send_transactional_email."""
    try:
        with contained_read(db):
            row = db.query(AppSettings).first()
        return (getattr(row, "automation_sender_user_id", None) or None) if row else None
    except Exception:
        log.exception("winback_drain_sender_read_failed")
        return None


def _send_email(db: Session, campaign: WinbackCampaign, send: WinbackSend, customer: Customer) -> tuple[str, str | None]:
    from gdx_dispatch.core.email_layout import email_branding, linkify, nl2br, render_email
    from gdx_dispatch.core.email_recipients import resolve_recipient
    from gdx_dispatch.core.transactional_email import send_transactional_email

    if customer.email_opt_out:
        return "skipped", "email_opt_out"
    resolved = resolve_recipient(db, customer, None)
    if not resolved.ok:
        return "skipped", "no_recipient_email"
    text = render_body(campaign.body_template, customer)
    if not text.strip():
        return "skipped", "empty_body"
    branding = email_branding(db)
    accent = branding.get("accent") or "#2563eb"
    body_html = (
        '<p style="margin:0 0 12px;">'
        + linkify(nl2br(text), accent).replace("<br><br>", '</p><p style="margin:0 0 12px;">')
        + "</p>"
    )
    subject = (campaign.subject or "").strip() or campaign.name
    html = render_email(branding=branding, body_html=body_html, title=subject, preheader=subject)
    sent, _provider, skip_reason = send_transactional_email(
        tenant_db=db,
        tenant_id=str(send.company_id or ""),
        user_id=_automation_sender(db),
        to_email=resolved.email,
        to_name=resolved.to_name,
        subject=subject,
        html_body=html,
        initiator_kind="system",
        initiator_ref="winback_drain",
        kind="winback",
        entity_type="winback_campaign",
        entity_id=str(campaign.id),
        recipient_source=resolved.source,
        recipient_contact_id=resolved.contact_id,
    )
    return ("sent", None) if sent else ("failed", skip_reason or "send_failed")


def _never_reached(exc: Exception) -> bool:
    """Phone.com provably did not act: the client's own retry rule for a
    request whose repeat would be a second text."""
    return getattr(exc, "status_code", None) == 429 or isinstance(
        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    )


def _send_sms(db: Session, campaign: WinbackCampaign, send: WinbackSend, customer: Customer) -> tuple[str, str | None]:
    """A Phone.com text with the link engine's rules: the message row is
    committed before Phone.com is called, and an ambiguous outcome is
    ``unknown``, never retried. One attempt, waited on for WORST_SEND at
    most: the client's own retries (up to five, with backoff) are off, and a
    text Phone.com provably never got is handed back as NOT_REACHED."""
    from fastapi import HTTPException

    from gdx_dispatch.core import modules as core_modules
    from gdx_dispatch.core.link_sms import adopt_message_id, definitely_not_sent, recipient
    from gdx_dispatch.modules.phone_com.client import PhoneComAPIError
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage
    from gdx_dispatch.modules.phone_com.outbound_did import resolve_outbound_did
    from gdx_dispatch.modules.phone_com.router import _get_phone_com_client, _thread_key_for

    if not core_modules.is_module_enabled("phone_com", _WorkerRequest(send.company_id), db):
        return "skipped", "texting_disabled"
    to, refusal = recipient(customer, None)
    if refusal:
        return "skipped", refusal["code"]
    body = render_body(campaign.body_template, customer)
    if not body.strip():
        return "skipped", "empty_body"
    from_number = resolve_outbound_did(db, customer_id=customer.id, to_number=to)
    if not from_number:
        return "failed", "no_outbound_number"
    try:
        client = _get_phone_com_client(UUID(str(send.company_id)), db, db)
    except (HTTPException, ValueError):
        return "failed", "phone_com_not_configured"
    if len(body) > client._SMS_MAX_BODY:
        return "failed", "body_too_long"
    client.retry_max_attempts = 0
    client._client.timeout = SEND_TIMEOUT

    msg = PhoneComMessage(
        id=uuid4(),
        phone_com_message_id=f"pending-{uuid4()}",
        thread_key=_thread_key_for(from_number, to),
        direction="out",
        from_number=from_number,
        to_number=to,
        body=body,
        sent_at=utcnow(),
        delivery_status="sending",
        attachments=[],
        customer_id=customer.id,
        raw_payload={"winback_send_id": str(send.id)},
    )
    db.add(msg)
    db.commit()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="winback_sms")
    try:
        result = pool.submit(client.send_message, from_number=from_number, to_number=to, body=body).result(
            timeout=WORST_SEND.total_seconds()
        )
    except FuturesTimeout:
        # Still in flight: it may yet arrive. The thread finishes on its own.
        msg.delivery_status = "unknown"
        msg.delivery_failed_reason = f"no answer within {WORST_SEND.total_seconds():g}s"
        db.commit()
        return "unknown", "sms_outcome_unknown"
    except (ValueError, TypeError):
        result = {}  # a 2xx whose body would not parse: Phone.com accepted it
    except (PhoneComAPIError, httpx.HTTPError) as exc:
        db.rollback()
        definite = definitely_not_sent(exc)
        msg.delivery_status = "failed" if definite else "unknown"
        msg.delivery_failed_reason = str(exc)[:500]
        db.commit()
        if _never_reached(exc):
            return NOT_REACHED, f"sms_not_reached: {exc}"[:500]
        return ("failed", f"sms_provider_error: {exc}"[:500]) if definite else ("unknown", "sms_outcome_unknown")
    finally:
        pool.shutdown(wait=False)
    if not isinstance(result, dict):
        result = {}
    adopt_message_id(db, msg, str(result.get("id") or msg.phone_com_message_id))
    msg.delivery_status = result.get("status") or "queued"
    msg.raw_payload = {**result, "winback_send_id": str(send.id)}
    db.commit()
    return "sent", None


def _deliver(db: Session, campaign: WinbackCampaign, send: WinbackSend) -> tuple[str, str | None]:
    customer = db.execute(
        select(Customer).where(Customer.id == send.customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    if customer is None:
        return "skipped", "customer_not_found"
    if send.channel == "email":
        return _send_email(db, campaign, send, customer)
    if send.channel == "sms":
        return _send_sms(db, campaign, send, customer)
    return "skipped", f"unknown_channel:{send.channel}"


def claim(db: Session, send_id: UUID, *, now=None) -> bool:
    """True only for the one caller that moved the row out of ``queued``.
    Committed before anything is sent. The ORM binds ``send_id`` through the
    ``Uuid`` type, so the guard matches SQLite's dashless hex too."""
    won = db.execute(
        update(WinbackSend)
        .where(WinbackSend.id == send_id, WinbackSend.status == "queued")
        .values(status="sending", sent_at=now or utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return won == 1


def _finish(db: Session, send_id: UUID, status: str, error: str | None) -> None:
    db.execute(
        update(WinbackSend)
        .where(WinbackSend.id == send_id, WinbackSend.status == "sending")
        .values(status=status, error=error, sent_at=utcnow() if status == "sent" else None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def reap_stale(db: Session, *, now=None) -> Counter:
    """Rows a killed worker left ``sending`` become ``unknown``, never resent."""
    now = now or utcnow()
    stale = db.execute(
        select(WinbackSend.id, WinbackSend.campaign_id).where(
            WinbackSend.status == "sending", WinbackSend.sent_at < now - STALE_SENDING
        )
    ).all()
    reaped: Counter = Counter()
    for send_id, campaign_id in stale:
        won = db.execute(
            update(WinbackSend)
            .where(WinbackSend.id == send_id, WinbackSend.status == "sending")
            .values(status="unknown", error="worker_interrupted", sent_at=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if won == 1:
            reaped[campaign_id] += 1
    return reaped


def _settle(db: Session, campaign_id: UUID) -> str | None:
    """Close a ``sending`` campaign with no open rows; returns its new status."""
    open_rows = db.execute(
        select(func.count()).select_from(WinbackSend).where(
            WinbackSend.campaign_id == campaign_id, WinbackSend.status.in_(OPEN_STATUSES)
        )
    ).scalar_one()
    if open_rows:
        return None
    counts = dict(
        db.execute(
            select(WinbackSend.status, func.count())
            .where(WinbackSend.campaign_id == campaign_id)
            .group_by(WinbackSend.status)
        ).all()
    )
    # `failed` only when provably nobody was contacted: an `unknown` row may
    # have reached its customer, so it counts with `sent` (the counts say which).
    final = "sent" if counts.get("sent") or counts.get("unknown") else "failed"
    won = db.execute(
        update(WinbackCampaign)
        .where(WinbackCampaign.id == campaign_id, WinbackCampaign.status == "sending")
        .values(status=final, sent_at=utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if won != 1:
        return None
    tenant_id = db.execute(select(WinbackCampaign.company_id).where(WinbackCampaign.id == campaign_id)).scalar_one()
    _audit(db, tenant_id, f"winback_campaign_{final}", campaign_id, {"counts": counts})
    return final


def drain(db: Session, *, now=None) -> dict[str, int]:
    """Deliver up to BATCH_SIZE rows, then settle every finished campaign.
    One row's failure never blocks the batch."""
    started = utcnow()
    deadline = started + DRAIN_BUDGET
    now = now or started
    per_campaign: dict[UUID, Counter] = {}
    for campaign_id, n in reap_stale(db, now=now).items():
        per_campaign.setdefault(campaign_id, Counter())["unknown"] += n

    sending = select(WinbackCampaign.id).where(
        WinbackCampaign.status == "sending", WinbackCampaign.deleted_at.is_(None)
    )
    due = db.execute(
        select(WinbackSend.id, WinbackSend.channel)
        .where(WinbackSend.status == "queued", WinbackSend.campaign_id.in_(sending))
        .order_by(WinbackSend.created_at)
        .limit(BATCH_SIZE)
    ).all()

    totals: Counter = Counter()
    texting_paused = False
    for send_id, channel in due:
        if deadline - utcnow() < WORST_SEND:
            break  # one more text might not finish in budget
        if texting_paused and channel == "sms":
            continue  # Phone.com just refused one; email rows still go
        if not claim(db, send_id):
            continue  # another drain got it
        send = db.get(WinbackSend, send_id)
        campaign = db.get(WinbackCampaign, send.campaign_id)
        prior_tries = _not_reached_tries(send.error)
        try:
            status, error = _deliver(db, campaign, send)
        except Exception as exc:  # noqa: BLE001 — one row must not stop the batch
            with contextlib.suppress(Exception):
                db.rollback()
            log.exception("winback_drain_deliver_crashed send=%s", send_id)
            # It crashed after the claim, possibly mid-transport: not provably unsent.
            status, error = "unknown", f"exception: {type(exc).__name__}"
        counted = status
        if status == NOT_REACHED:
            texting_paused = True  # rate-limited or unreachable: texts wait for the next pass
            tries = prior_tries + 1
            reason = (error or "").removeprefix("sms_not_reached: ")
            if tries >= NOT_REACHED_TRIES:
                status = counted = "failed"
                error = f"sms_not_reached after {tries} tries: {reason}"
            else:
                counted = "not_reached"
                error = f"sms_not_reached (try {tries} of {NOT_REACHED_TRIES}): {reason}"
        _finish(db, send_id, status, (error or None) and error[:1000])
        per_campaign.setdefault(campaign.id, Counter())[counted] += 1
        totals[counted] += 1

    tenants = dict(
        db.execute(
            select(WinbackCampaign.id, WinbackCampaign.company_id).where(WinbackCampaign.id.in_(list(per_campaign)))
        ).all()
    ) if per_campaign else {}
    for campaign_id, counts in per_campaign.items():
        _audit(db, tenants.get(campaign_id, ""), "winback_sends_drained", campaign_id, {"counts": dict(counts)})

    settled = 0
    for campaign_id in db.execute(sending).scalars().all():
        if _settle(db, campaign_id):
            settled += 1
    return {"claimed": sum(totals.values()), **dict(totals), "settled": settled}


@celery_app.task(name="gdx_dispatch.tasks.winback_drain.drain_winback_sends")
def drain_winback_sends() -> dict[str, int]:
    """Beat task, every minute. No-ops when no campaign is ``sending``."""
    with contextlib.closing(SessionLocal()) as db:
        result = drain(db)
    if result["claimed"] or result["settled"]:
        log.info("winback_drain %s", result)
    return result
