"""The event handler — the whole point of this plugin.

plugin-host invokes this for every domain event the owner consented to. It runs
IN the plugin-host process (request-free), so it opens its own session. Idempotent
on delivery_id: the platform is at-least-once, so a re-delivery must not create a
duplicate row.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.exc import IntegrityError

from gdx_dispatch.core.database import SessionLocal
from gdx_plugin_eventlog.models import EventLogEntry

log = logging.getLogger(__name__)


def handle_event(evt) -> None:
    """evt is a gdx_dispatch.plugin_api.events.PluginEvent."""
    if not evt.tenant_id:
        return
    db = SessionLocal()
    try:
        row = EventLogEntry(
            company_id=str(evt.tenant_id),
            event_name=str(evt.name),
            delivery_id=(str(evt.delivery_id) or None) or None,
            payload_json=json.dumps(evt.data, default=str)[:8000],
        )
        db.add(row)
        db.commit()
    except IntegrityError:
        # Duplicate delivery_id → already recorded (at-least-once). No-op.
        db.rollback()
    except Exception:
        log.exception("eventlog_handle_failed event=%s", getattr(evt, "name", "?"))
        db.rollback()
    finally:
        db.close()
