"""Event handler: a new/changed customer claims their unlinked texts and calls.

Runs in the plugin-host process for `customer.created` / `customer.updated`
(request-free, own session — same pattern as gdx-plugin-eventlog's handler).
The event payload is PII-minimized (no phone number), so this doesn't match on
the payload at all — it just triggers a rematch pass over the tenant's
unlinked rows, which reads phone_hash from the DB. That closes the "customer
created the day AFTER their first text stays unlinked forever" hole the
sms-caller-identity plan documents, without needing a scheduler (the plugin
`schedules` declaration is catalogued but nothing runs it today).

Rematch is idempotent, so at-least-once delivery needs no delivery_id dedupe —
a re-run finds nothing left to link.
"""
from __future__ import annotations

import logging

from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def handle_customer_event(evt) -> None:
    """evt is a gdx_dispatch.plugin_api.events.PluginEvent."""
    if not evt.tenant_id:
        return
    from gdx_plugin_cellcomms.matching import rematch_unlinked

    db = SessionLocal()
    try:
        result = rematch_unlinked(db, str(evt.tenant_id))
        if result.get("linked"):
            log.info("cellcomms_rematch event=%s linked=%d", evt.name, result["linked"])
    except Exception:
        log.exception("cellcomms_rematch_failed event=%s", getattr(evt, "name", "?"))
        db.rollback()
    finally:
        db.close()
