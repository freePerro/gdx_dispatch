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

import contextlib
import contextvars
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
# Envelopes to fan out to consented PLUGINS (the WordPress-model hook), dispatched
# on the same after_commit as the tenant webhooks.
_PLUGIN_PENDING_KEY = "_gdx_pending_plugin_dispatch"

_hook_installed = False

# Backfill/importer suppression. A programmatic paid-status backfill that routes
# through the choke points (transition_invoice_status) can wrap its loop in
# `with suppress_domain_events():` to avoid firing an invoice.paid per years-old
# invoice. QB *sync* already bypasses the choke points entirely (writes status
# directly). NOTE: this only silences IN-PROCESS callers — a manual backfill
# driven through the record_payment HTTP endpoint cannot wrap the handler, so
# those emits still fire; suppressing them needs a request-level opt-out (future).
_suppress_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "gdx_suppress_domain_events", default=False
)


@contextlib.contextmanager
def suppress_domain_events():
    """Silence domain-event emission for the duration of the block (nestable)."""
    token = _suppress_var.set(True)
    try:
        yield
    finally:
        _suppress_var.reset(token)


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
    if suppress or _suppress_var.get() or not tenant_id:
        return 0

    # A choke point that emits sits in the middle of a business write (invoicing,
    # job creation). Webhook fan-out must NEVER break that write — the entire
    # body is guarded, and per-row staging is isolated by a SAVEPOINT, so any
    # emit failure degrades to "no webhook" rather than a failed payment.
    try:
        return _emit(db, str(tenant_id), event_type, entity_id, data)
    except Exception:
        log.exception("emit_domain_event_failed event=%s entity=%s", event_type, entity_id)
        return 0


def _emit(db: Session, tenant_id: str, event_type: str, entity_id: str, data: dict[str, Any]) -> int:
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
        # Known edge (at-least-once tradeoff): a genuine re-event for the same
        # (entity, event, subscription) — e.g. an invoice reopened then re-paid —
        # reuses this key and the 2nd delivery is deduped away. Acceptable for
        # v1; callers needing every occurrence pass a distinct entity_id.
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
        # event for the same entity+subscription) raises IntegrityError; any
        # other per-row error is likewise isolated here. Either way the dup/error
        # degrades to a no-op and the caller's transaction is untouched.
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
        except Exception:
            log.exception("webhook_delivery_stage_failed event=%s sub=%s", event_type, sub.id)
            continue
        staged.append(str(row.id))

    if staged:
        db.info.setdefault(_PENDING_KEY, []).extend(staged)

    # Plugin sink: the SAME event goes to consented plugins via plugin-host. Gate
    # on a cheap consent check so a zero-plugin box (the common case) stages
    # nothing and fires no task. The recipient decision is made later, in the
    # task, from the stored consent preimage (drift-checked) — core-owned routing.
    try:
        from gdx_dispatch.core.plugin_consent import any_event_consent

        if any_event_consent(db):
            db.info.setdefault(_PLUGIN_PENDING_KEY, []).append(
                {
                    "event": event_type,
                    "data": data,
                    "tenant_id": tenant_id,
                    "occurred_at": envelope["occurred_at"],
                    "delivery_id": hashlib.sha256(
                        f"{tenant_id}:{event_type}:{entity_id}:{envelope['occurred_at']}".encode()
                    ).hexdigest(),
                }
            )
    except Exception:
        log.exception("plugin_dispatch_stage_failed event=%s", event_type)

    if staged or db.info.get(_PLUGIN_PENDING_KEY):
        _install_dispatch_hook()
    return len(staged)


def _dispatch_pending(session: Session) -> None:
    """after_commit: staged work is now durable — enqueue delivery. Only ever
    enqueues Celery tasks; never touches the DB on the just-committed session."""
    ids = session.info.pop(_PENDING_KEY, None)
    if ids:
        # Import here: this module is imported early (via choke points) and the
        # task module pulls in the Celery app — avoid an import cycle at load.
        from gdx_dispatch.core.webhooks.tasks import deliver_webhook_task

        for delivery_id in ids:
            try:
                deliver_webhook_task.delay(delivery_id)
            except Exception:
                # Broker unreachable: the row is committed 'pending' with
                # next_retry_at=NULL; the retry sweep rescues exactly those.
                log.exception("webhook_dispatch_enqueue_failed id=%s", delivery_id)

    envelopes = session.info.pop(_PLUGIN_PENDING_KEY, None)
    if envelopes:
        from gdx_dispatch.core.plugin_events import deliver_plugin_event_task

        for envelope in envelopes:
            try:
                deliver_plugin_event_task.delay(envelope)
            except Exception:
                # Best-effort (no plugin retry ledger in v1); the event is simply
                # not delivered to plugins if the broker is down.
                log.exception("plugin_dispatch_enqueue_failed event=%s", envelope.get("event"))


def _drop_pending(session: Session, previous_transaction) -> None:  # noqa: ANN001
    """Forget staged dispatch when the BUSINESS transaction rolls back, so a later
    commit on the same session can't dispatch phantoms.

    Uses after_soft_rollback (not after_rollback) and skips SAVEPOINT rollbacks
    (`previous_transaction.nested`): emit itself rolls back savepoints as normal
    control flow — a duplicate-key skip in the fan-out, and the read-only
    plugin-consent probe on a fresh box (missing table). after_rollback fires on
    those too, and clearing pending there dropped legitimately-staged deliveries
    — webhooks silently never fired on a fresh Postgres box. Only a real
    (non-nested) rollback means the business write is gone with its deliveries."""
    if getattr(previous_transaction, "nested", False):
        return  # savepoint rolled back; the outer business txn is still live
    session.info.pop(_PENDING_KEY, None)
    session.info.pop(_PLUGIN_PENDING_KEY, None)


def _install_dispatch_hook() -> None:
    """Idempotent, process-global. Listening on the Session *class* covers every
    session this process creates (mirrors install_flush_guard)."""
    global _hook_installed
    if _hook_installed:
        return
    event.listen(Session, "after_commit", _dispatch_pending)
    # after_soft_rollback (not after_rollback) so we can tell a SAVEPOINT rollback
    # — emit's own normal control flow — from a real business-txn rollback.
    event.listen(Session, "after_soft_rollback", _drop_pending)
    _hook_installed = True


# Public alias for startup wiring (app + celery import this to arm the hook
# unconditionally, so even the very first emit dispatches).
install_webhook_dispatch_hook = _install_dispatch_hook
