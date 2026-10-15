"""Text the customer that the tech is on the way (GDXA-1201).

The one helper both en-route routes call — the tech's ``POST
/api/mobile/jobs/{id}/en-route`` and the office's ``POST
/api/appointments/{id}/on-my-way`` — so the two cannot drift on who gets a
text, or when.

It does not call Phone.com. It queues the text on the scheduled-text queue
(``modules/phone_com/scheduled.py``) due *now*, and the every-minute drain
sends it with every rule that queue already guarantees: a pending message row
before Phone.com is called, never re-sent after an ambiguous outcome, the
customer's opt-out and the texting kill switch re-checked at send time, an
audit row per outcome, and an office-bell alert for a text that did not go.
So the en-route write (load-bearing for time tracking) never waits on, or
fails with, Phone.com; the text lands within about a minute.

Contract for callers:

* call it AFTER the en-route write has committed. It refuses to run on a
  session holding uncommitted ORM work, pending or flushed
  (``caller_uncommitted``), rather than commit or roll back someone else's
  work. An uncommitted Core-level write it cannot see: it would be committed
  by a send or skip and rolled back by an error, so don't leave one;
* it never raises. Every outcome is an ``OnMyWayResult``;
* report ``customer_notified=result.queued`` — True only when a text was
  queued by this call.

Skips (``reason``), each audited as ``on_my_way_sms_skipped`` except the first two:
``disabled`` (the tenant has not opted in — not an attempt, so no audit row),
``caller_uncommitted`` (not audited either: auditing would commit the
caller's work), ``no_user``, ``texting_disabled``, ``no_customer``, ``sms_opt_out``,
``no_valid_phone``, ``no_outbound_number``, ``already_notified`` (a text for
this job — or, with no job, this customer — was queued or sent within
``DEDUPE_WINDOW``: a re-tap or a second tech must not text twice).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# ScheduledSms.audit_action for these rows: the drain writes it on the send,
# and the dedupe below finds earlier texts by it.
ACTION_ON_MY_WAY = "on_my_way_sms"
# Long enough that a re-tap, a second tech or the office pressing the button
# after the tech did sends nothing more; short enough that day 2 of a
# multi-day job texts again.
DEDUPE_WINDOW = timedelta(hours=4)
# Statuses that mean no text reached the customer — they never block another.
_NOT_DELIVERED = ("failed", "skipped", "canceled")


@dataclass(frozen=True)
class OnMyWayResult:
    queued: bool
    reason: str | None = None
    scheduled_sms_id: str | None = None


def is_enabled(db: Session) -> bool:
    from gdx_dispatch.models.tenant_models import AppSettings

    row = db.query(AppSettings).first()
    return bool(row and getattr(row, "on_my_way_sms_enabled", False))


def _tech_name(db: Session, actor: UUID) -> str:
    from gdx_dispatch.models.tenant_models import User

    u = db.execute(select(User.full_name, User.name).where(User.id == actor)).first()
    full = ((u.full_name or u.name) if u else "") or ""
    return full.strip().split(" ")[0] if full.strip() else ""


def render_body(*, customer_name: str, tech_name: str, company: str, eta_minutes: int | None) -> str:
    """The one wording. Deliberately not the ``on_my_way`` notification
    template: no settings screen edits that row, and its create route is open
    to any signed-in user, so reading it would let anyone rewrite what every
    customer is texted."""
    first = (customer_name or "").strip().split(" ")[0] if (customer_name or "").strip() else ""
    greeting = f"Hi {first}, " if first else "Hi, "
    who = tech_name or "your technician"
    sender = f" from {company}" if company else ""
    eta = f" and should be there in about {eta_minutes} minutes" if eta_minutes else ""
    return f"{greeting}{who}{sender} is on the way{eta}."


def _caller_has_uncommitted_work(db: Session) -> bool:
    """Pending ORM changes, or ORM changes flushed in the open transaction but
    not committed. The flushed half reads the session transaction's own bookkeeping
    (``_new``/``_dirty``/``_deleted``, what a rollback uses to expire objects)
    up the nesting chain. A Core-level write (``db.execute(update(...))``) left
    uncommitted is NOT visible here — that is on the caller's contract."""
    if db.new or db.dirty or db.deleted:
        return True
    tx = db.get_nested_transaction() or db.get_transaction()
    while tx is not None:
        if getattr(tx, "_new", None) or getattr(tx, "_dirty", None) or getattr(tx, "_deleted", None):
            return True
        tx = tx.parent
    return False


def _already_notified(db: Session, *, job_id: UUID | None, customer_id: UUID, now: datetime) -> bool:
    from gdx_dispatch.modules.phone_com.models import ScheduledSms

    q = select(ScheduledSms.id).where(
        ScheduledSms.audit_action == ACTION_ON_MY_WAY,
        ScheduledSms.created_at >= now - DEDUPE_WINDOW,
        ScheduledSms.status.not_in(_NOT_DELIVERED),
    )
    q = q.where(ScheduledSms.job_id == job_id) if job_id else q.where(ScheduledSms.customer_id == customer_id)
    return db.execute(q.limit(1)).first() is not None


def send_on_my_way(
    db: Session,
    *,
    user: dict[str, Any],
    request: Any,
    job_id: UUID | str | None = None,
    customer_id: UUID | str | None = None,
    eta_minutes: int | None = None,
) -> OnMyWayResult:
    """Queue the on-my-way text for ``job_id`` (its customer), or for
    ``customer_id`` when there is no job. ``user`` is the signed-in caller —
    the text is sent as them. See the module docstring for the contract."""
    from gdx_dispatch.core.audit import audit_best_effort
    from gdx_dispatch.core.link_sms import as_uuid

    if _caller_has_uncommitted_work(db):
        log.error("on_my_way_sms called with uncommitted changes job=%s", job_id)
        return OnMyWayResult(False, "caller_uncommitted")

    job_uuid = as_uuid(job_id)
    actor = as_uuid((user or {}).get("user_id") or (user or {}).get("sub"))
    tenant_id: str | None = None

    def _audit_not_queued(action: str, **details: Any) -> None:
        audit_best_effort(
            db,
            action=action,
            entity_type="job" if job_uuid else "customer",
            entity_id=str(job_uuid or customer_id or ""),
            tenant_id=tenant_id,
            user_id=str(actor) if actor else None,
            request=request,
            details={"channel": "sms", **details},
        )

    def _skip(reason: str, **extra: Any) -> OnMyWayResult:
        _audit_not_queued("on_my_way_sms_skipped", reason=reason, **extra)
        return OnMyWayResult(False, reason)

    try:
        if not is_enabled(db):
            return OnMyWayResult(False, "disabled")
        from gdx_dispatch.core.link_sms import active_customer, company_name, recipient, tenant_uuid

        tenant_id = str(tenant_uuid(user, request))
        if actor is None:
            # The drain sends as a person and re-checks that person at send
            # time; with nobody signed in there is no one to answer for it.
            return _skip("no_user")

        from gdx_dispatch.core import modules as core_modules
        from gdx_dispatch.modules.phone_com.scheduled import _WorkerRequest

        if not core_modules.is_module_enabled("phone_com", _WorkerRequest(tenant_id), db):
            return _skip("texting_disabled")

        cust_id = as_uuid(customer_id)
        if job_uuid is not None:
            from gdx_dispatch.models.tenant_models import Job

            job_customer = db.execute(
                select(Job.customer_id).where(Job.id == job_uuid, Job.deleted_at.is_(None))
            ).scalar_one_or_none()
            cust_id = as_uuid(job_customer) or cust_id
        customer = active_customer(db, cust_id)
        to, refusal = recipient(customer, None)
        if refusal:
            return _skip(refusal["code"], customer_id=str(cust_id) if cust_id else None)

        from gdx_dispatch.modules.phone_com.outbound_did import resolve_outbound_did

        if not resolve_outbound_did(db, customer_id=customer.id, to_number=to, sending_user_id=actor):
            return _skip("no_outbound_number", customer_id=str(customer.id))

        now = datetime.now(UTC)
        if _already_notified(db, job_id=job_uuid, customer_id=customer.id, now=now):
            return _skip("already_notified", customer_id=str(customer.id))

        body = render_body(
            customer_name=customer.name,
            tech_name=_tech_name(db, actor),
            company=company_name(db),
            eta_minutes=eta_minutes,
        )

        from gdx_dispatch.core.audit import audit_or_rollback
        from gdx_dispatch.modules.phone_com.scheduled import KIND_MESSAGE, new_row

        # Due now — the next drain (every minute) sends it. Not through
        # scheduled.create(): that refuses anything under a minute ahead.
        row = new_row(
            kind=KIND_MESSAGE, to=to, body=body, send_at=now, audit_action=ACTION_ON_MY_WAY,
            tenant_id=tenant_id, actor=actor, customer_id=customer.id, job_id=job_uuid,
        )
        db.add(row)
        db.flush()
        audit_or_rollback(
            db,
            action="on_my_way_sms_queued",
            entity_type="job" if job_uuid else "customer",
            entity_id=str(job_uuid or customer.id),
            tenant_id=tenant_id,
            request=request,
            actor={"user_id": str(actor)},
            details={
                "channel": "sms",
                "scheduled_sms_id": str(row.id),
                "customer_id": str(customer.id),
                "to_last4": to[-4:],
                "eta_minutes": eta_minutes,
            },
        )
        db.commit()
        return OnMyWayResult(True, None, str(row.id))
    except Exception as exc:  # noqa: BLE001 — a text must never fail the en-route write
        log.exception("on_my_way_sms_failed job=%s", job_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            log.exception("on_my_way_sms_rollback_failed job=%s", job_id)
        _audit_not_queued("on_my_way_sms_failed", error=type(exc).__name__)
        return OnMyWayResult(False, "error")
