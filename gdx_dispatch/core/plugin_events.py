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
import time

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


# The re-arm check below fetches the catalog over HTTP. It runs on a drifted
# plugin's event/schedule path, so it is attempted at most this often per
# process rather than on every tick (GDXA-462 audit).
_REARM_INTERVAL_S = 300.0
_last_rearm_check = float("-inf")


def _signal_consent_drift(db, drifted: list[str], event_name: str) -> None:
    """Record that a plugin's automation is suppressed because its declared
    events changed since consent. THROTTLED: only signals when no drift record
    is already pending, so a high-volume event stream can't flood the log/table.

    Honest scope: v1 surfaces this two ways. The ops alarm is a log.error
    marked ``ops_alert`` with fingerprint ``plugin-events``. Once the GDXA-269
    ops-alert handler is installed, the error sink records each marked record
    as a ``server_errors`` row and, when ``OPS_ALERT_EMAIL`` is set, emails
    the maintainer (at most once per fingerprint per hour, daily-capped);
    until then it is a container-log line only. The other is a pending
    `plugin_consent_drift` AIAction row. The throttle makes the alarm fire
    once per pending row. A row whose drift has provably ended is marked
    `resolved` (`resolve_drift_signals`, GDXA-462), which re-arms the alarm:
    when the owner re-consents (audited, in the consent route), and here,
    when a NEW drift arrives while a row naming only other plugins is still
    pending (their uninstall or rollback). While a row whose plugins are
    still drifted is pending, a later drift raises no new alarm. The
    owner-facing signal is the re-consent banner on the Plugins admin page,
    which reads the LIVE drift (`GET /api/admin/plugins/consent-drift`), not
    this throttled row."""
    from gdx_dispatch.core.webhooks.models import AIAction

    try:
        open_rows = db.execute(
            select(AIAction.payload).where(
                AIAction.action_type == "plugin_consent_drift",
                AIAction.status == "pending",
            )
        ).scalars().all()
        if open_rows:
            if all(set((p or {}).get("plugins") or []) & set(drifted) for p in open_rows):
                return  # one open drift flag is enough; don't re-signal per event
            global _last_rearm_check
            now = time.monotonic()
            if now - _last_rearm_check < _REARM_INTERVAL_S:
                return
            _last_rearm_check = now
            from gdx_dispatch.core.plugin_consent import fetch_catalog, resolve_drift_signals

            closed = resolve_drift_signals(db, fetch_catalog())
            if closed:
                log.warning("plugin_consent_drift: closed ended alarm rows %s "
                            "(system, on new drift of %s)", closed, drifted)
                db.commit()
            if len(closed) < len(open_rows):
                return  # a row for a still-drifted plugin is pending
        log.error(
            "plugin_consent_drift: dispatch suppressed for %s (first seen on "
            "event=%s) — plugin changed its declared events since consent; owner "
            "must re-consent",
            drifted, event_name,
            extra={"ops_alert": True, "ops_fingerprint": "plugin-events"},
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
            raise self.retry(exc=exc) from exc
        if r.status_code >= 500:
            raise self.retry(exc=RuntimeError(f"plugin-host {r.status_code}"))
        if r.status_code in (401, 403):
            # Not a plugin problem and not transient: THIS container is missing
            # GDX_INTERNAL_TOKEN, or has the wrong one. Retrying cannot help.
            #
            # This used to fall into the generic 4xx branch below, which logged
            # at WARNING and returned 0 — a task that reports success while
            # silently dropping every plugin event for as long as the
            # misconfiguration lasts. ERROR, and name the fix, because the
            # symptom (nothing happens) is otherwise indistinguishable from
            # "no plugin was interested".
            log.error(
                "plugin_event_dispatch_unauthorized event=%s status=%s recipients=%s — "
                "GDX_INTERNAL_TOKEN is missing or wrong in THIS container. "
                "plugin-host fails closed on it (#596); the compose files have no "
                "env_file, so the variable must be in the shared x-app-env block. "
                "Every event to these plugins is being dropped.",
                event_name, r.status_code, recipients,
            )
            return 0
        if r.status_code >= 300:
            # Other 4xx is not retryable — log and drop.
            log.warning("plugin_event_dispatch_http_%s event=%s", r.status_code, event_name)
            return 0
    return len(recipients)


# The schedule driver's Celery task is named under this module (so the
# plugin_events route sends it to priority:high) and is registered by importing
# it here, because the worker's include list names this module. A module import,
# not a name import, so either import order works.
import gdx_dispatch.core.plugin_schedules  # noqa: E402,F401
