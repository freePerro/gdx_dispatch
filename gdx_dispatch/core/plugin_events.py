"""Core → plugin-host domain-event dispatch (Celery).

The tenant-webhook path (core/webhooks) delivers to external HTTP receivers with
a durable delivery ledger + retry sweep. This is the sibling path that delivers
the SAME domain events to *installed plugins* whose owner consented — the
WordPress-model hook. Core owns the routing decision: it enumerates recipients
from the STORED consent preimage (drift-checked against the live catalog) and
POSTs one event to plugin-host's token-gated `/internal/events` with an explicit
recipient list. plugin-host never decides who receives — it delivers to whom it's
told (and re-checks the pattern match).

Delivery guarantee, stated honestly: **best-effort, at-least-once once enqueued.**
The Celery task retries a transiently-unreachable plugin-host (bounded), and
handlers dedupe on delivery_id. But there is NO durable per-plugin ledger — if
the broker is down at enqueue time (the after_commit hook in emit.py), or the
task exhausts retries, the event is dropped for plugins with only a logged
failure. Reliability-critical automations should use the durable tenant-webhook
path. A per-plugin ledger is deferred (plan open question).

Consent DRIFT (a plugin changed its declared events since consent) fail-closes
dispatch and records a throttled owner signal — a silent stop of automation is
exactly the footgun the audit called out.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.plugin_consent import (
    _plugin_host_url,
    event_recipients,
    internal_auth_headers,
)

log = logging.getLogger(__name__)


def _signal_consent_drift(db, drifted: list[str], event_name: str) -> None:
    """Record that a plugin's automation is suppressed because its declared
    events changed since consent. THROTTLED: only signals when no drift record
    is already pending, so a high-volume event stream can't flood the log/table.

    Honest scope: v1 surfaces this as an ERROR log (→ Sentry when configured) +
    a pending `plugin_consent_drift` AIAction row. An owner-facing banner/bell
    that reads that row is Sprint-2b work (frontend); this is NOT yet a UI
    signal, so do not claim it is."""
    from gdx_dispatch.core.webhooks.models import AIAction

    try:
        already = db.execute(
            select(AIAction.id).where(
                AIAction.action_type == "plugin_consent_drift",
                AIAction.status == "pending",
            ).limit(1)
        ).first()
        if already:
            return  # one open drift flag is enough; don't re-signal per event
        log.error(
            "plugin_consent_drift: dispatch suppressed for %s (first seen on "
            "event=%s) — plugin changed its declared events since consent; owner "
            "must re-consent",
            drifted, event_name,
        )
        db.add(
            AIAction(
                action_type="plugin_consent_drift",
                priority="high",
                payload={"plugins": drifted, "event": event_name},
                status="pending",
            )
        )
        db.commit()
    except Exception:
        log.exception("plugin_consent_drift_signal_failed")
        db.rollback()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def deliver_plugin_event_task(self, envelope: dict) -> int:
    """Deliver one domain event to consented plugin recipients. Returns the
    number of plugins the event was dispatched to. Retries a transiently
    unreachable plugin-host (bounded); gives up with a log after max_retries."""
    event_name = str(envelope.get("event") or "")
    if not event_name:
        return 0
    with SessionLocal() as db:
        recipients, drifted = event_recipients(db, event_name)
        if drifted:
            _signal_consent_drift(db, drifted, event_name)
        if not recipients:
            return 0
        body = {**envelope, "recipients": recipients}
        try:
            r = httpx.post(
                f"{_plugin_host_url()}/internal/events",
                json=body,
                headers=internal_auth_headers(),
                timeout=30.0,
            )
        except Exception as exc:
            # plugin-host transiently down → retry (bounded). Handlers dedupe on
            # delivery_id, so a retried delivery is safe.
            log.warning("plugin_event_dispatch_unreachable event=%s (retrying)", event_name)
            raise self.retry(exc=exc)
        if r.status_code >= 500:
            raise self.retry(exc=RuntimeError(f"plugin-host {r.status_code}"))
        if r.status_code >= 300:
            # 4xx (e.g. 401 token) is not retryable — log and drop.
            log.warning("plugin_event_dispatch_http_%s event=%s", r.status_code, event_name)
            return 0
    return len(recipients)
