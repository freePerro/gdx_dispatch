"""Domain-event emission — the live wire between business events and receivers.

Sprint 1 of the n8n / plugin-event platform (docs/design/n8n-automation-plan.md).

Two facts drove this module's shape, both from the adversarial audits:

1. **Fan-out must not break the business write.** The choke points that call
   ``emit_domain_event`` (e.g. ``transition_invoice_status``) run *inside* the
   caller's transaction and never commit themselves. So we cannot "commit then
   dispatch". Instead each delivery row is staged inside a ``begin_nested()``
   SAVEPOINT (an insert failure — e.g. a duplicate idempotency key on a
   legitimate re-emit — stays local and never poisons the caller's txn), and
   the Celery dispatch is deferred to an ``after_commit`` Session listener so it
   fires only once the business transaction actually commits. A rollback drops
   the staged dispatch with the rows.

2. **The receiver table is ``webhook_subscriptions``** — the one the CRUD API
   and the WebhooksView UI actually write. (The legacy ``webhook_endpoints``
   path stays wired through the existing ``emit_webhook``; this is the live
   customer path.)

Mirrors ``modules/ledger/guard.py``'s ``install_flush_guard``: one idempotent,
process-global listener on the ``Session`` *class* that drains a ``session.info``
list. The listener only ever enqueues Celery work — it never emits SQL on the
just-committed session.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.webhooks.models import WebhookDelivery
from gdx_dispatch.routers.webhooks import WebhookSubscription

log = logging.getLogger(__name__)

# session.info key holding delivery-row ids staged this transaction, dispatched
# on commit. Distinct, module-private key so nothing else drains it.
_PENDING_KEY = "_gdx_pending_webhook_dispatch"

_hook_installed = False


def build_envelope(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """The versioned wire envelope. ``delivery_id`` is injected at send time by
    the delivery worker (it isn't known until the row is flushed)."""
    return {
        "event": event_type,
        "data": data,
        "occurred_at": utcnow().isoformat(),
    }


def emit_domain_event(
    db: Session,
    event_type: str,
    entity_id: str,
    data: dict[str, Any],
    *,
    tenant_id: str | None,
    suppress: bool = False,
) -> int:
    """Stage webhook deliveries for every active subscription to ``event_type``.

    Returns the number of deliveries staged (0 if suppressed, no tenant, or no
    subscriber). Rows commit with the caller's transaction; actual HTTP dispatch
    fires from the ``after_commit`` hook. Never raises into the caller — a
    delivery-row failure is contained by a SAVEPOINT and logged.

    ``suppress=True`` is the importer/backfill escape hatch: a bulk paid-status
    backfill routes through the same choke points as live payments, and must not
    fire an ``invoice.paid`` per years-old invoice.
    """
    if suppress or not tenant_id:
        return 0

    tenant_id = str(tenant_id)
    envelope = build_envelope(event_type, data)
    staged: list[str] = []

    subs = (
        db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.company_id == tenant_id,
                WebhookSubscription.active.is_(True),
                WebhookSubscription.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for sub in subs:
        try:
            sub_events = json.loads(sub.events) if sub.events else []
        except (json.JSONDecodeError, TypeError):
            log.warning("webhook_subscription_bad_events id=%s", sub.id)
            continue
        if event_type not in sub_events:
            continue

        # Hash, do NOT truncate: real tenant/entity ids are UUIDs, so a raw
        # "{tenant}:{event}:{entity}:{sub}" string overruns the 100-char column
        # and a [:100] slice would chop the subscription-id suffix — re-merging
        # two receivers onto one key, the exact collision this key exists to
        # prevent (and the SAVEPOINT would then silently swallow the 2nd). The
        # 64-char digest is stable per (tenant, event, entity, subscription) and
        # always fits. It also travels back to the receiver as X-Idempotency-Key.
        idem = hashlib.sha256(
            f"{tenant_id}:{event_type}:{entity_id}:{sub.id}".encode()
        ).hexdigest()
        row = WebhookDelivery(
            company_id=tenant_id,
            subscription_id=str(sub.id),
            event_type=event_type,
            event=event_type,
            payload=envelope,
            url=sub.url,
            idempotency_key=idem,
            status="pending",
        )
        # SAVEPOINT: a duplicate idempotency_key (legitimate re-emit of the same
        # event for the same entity+subscription) raises IntegrityError here;
        # swallow it so the dup degrades to a no-op and the caller's transaction
        # is untouched. Receiver-side dedupe already covered the first delivery.
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            log.info(
                "webhook_delivery_duplicate_skipped event=%s entity=%s sub=%s",
                event_type, entity_id, sub.id,
            )
            continue
        staged.append(str(row.id))

    if staged:
        db.info.setdefault(_PENDING_KEY, []).extend(staged)
        _install_dispatch_hook()
    return len(staged)


def _dispatch_pending(session: Session) -> None:
    """after_commit: the staged rows are now durable — enqueue delivery. Only
    ever enqueues; never touches the DB on the just-committed session."""
    ids = session.info.pop(_PENDING_KEY, None)
    if not ids:
        return
    # Import here: this module is imported early (via choke points) and the task
    # module pulls in the Celery app — avoid an import cycle at module load.
    from gdx_dispatch.core.webhooks.tasks import deliver_webhook_task

    for delivery_id in ids:
        try:
            deliver_webhook_task.delay(delivery_id)
        except Exception:
            # Broker unreachable: the row is committed 'pending' with
            # next_retry_at=NULL; the retry sweep rescues exactly those.
            log.exception("webhook_dispatch_enqueue_failed id=%s", delivery_id)


def _drop_pending(session: Session) -> None:
    """after_rollback: the staged rows rolled back with the txn — forget them so
    a later commit on the same session can't dispatch phantoms."""
    session.info.pop(_PENDING_KEY, None)


def _install_dispatch_hook() -> None:
    """Idempotent, process-global. Listening on the Session *class* covers every
    session this process creates (mirrors install_flush_guard)."""
    global _hook_installed
    if _hook_installed:
        return
    event.listen(Session, "after_commit", _dispatch_pending)
    event.listen(Session, "after_rollback", _drop_pending)
    _hook_installed = True


# Public alias for startup wiring (app + celery import this to arm the hook
# unconditionally, so even the very first emit dispatches).
install_webhook_dispatch_hook = _install_dispatch_hook
