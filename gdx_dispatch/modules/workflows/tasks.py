"""Workflow-rule execution off the domain-event stream (email overhaul 4a).

Before this module, ``modules/workflows/engine.fire_trigger`` had ZERO
callers — the Automations UI could create rules and flip them active, but no
business event ever ran one. The emit hook (core/webhooks/emit.py) now stages
a workflow job whenever a SUPPORTED_TRIGGERS event fires with at least one
active rule subscribed, and this task drains it after the business commit —
the same lifecycle the plugin-event sink rides.

Delivery guarantee matches the plugin sink, honestly: best-effort once
enqueued (bounded retry on transient DB trouble); if the broker is down at
enqueue time the rules are skipped for that event with a logged error.
"""
from __future__ import annotations

import asyncio
import logging

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def run_workflow_rules_task(self, job: dict) -> int:
    """Run every active rule subscribed to this event. Returns rule count."""
    event_type = str(job.get("event_type") or "")
    tenant_id = str(job.get("tenant_id") or "")
    context = dict(job.get("context") or {})
    if not event_type:
        return 0
    try:
        from gdx_dispatch.modules.workflows.engine import fire_trigger

        with SessionLocal() as db:
            asyncio.run(fire_trigger(event_type, context, tenant_id, db))
        return 1
    except Exception as exc:
        log.warning("workflow_rules_task_retry event=%s", event_type)
        raise self.retry(exc=exc) from exc
