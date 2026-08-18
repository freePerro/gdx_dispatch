"""The event handler — records each consented business event into the plugin's
own table so the owner can see, in-app, what their n8n workflows can react to.

Runs IN the plugin-host process (request-free), so it opens its own session.
Idempotent on delivery_id: the platform is at-least-once, so a re-delivery must
not create a duplicate row. It deliberately does NOT call n8n — the plugin-host
can't reach the automation network; forwarding is the webhook path's job.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.exc import IntegrityError

from gdx_dispatch.core.database import SessionLocal
from gdx_plugin_n8n.models import N8nEvent

log = logging.getLogger(__name__)


def handle_event(evt) -> None:
    """evt is a gdx_dispatch.plugin_api.events.PluginEvent."""
    if not evt.tenant_id:
        return
    db = SessionLocal()
    try:
        row = N8nEvent(
            company_id=str(evt.tenant_id),
            event_name=str(evt.name),
            delivery_id=str(evt.delivery_id) if evt.delivery_id else None,
            payload_json=json.dumps(evt.data, default=str)[:8000],
        )
        db.add(row)
        db.commit()
    except IntegrityError:
        # Duplicate delivery_id → already recorded (at-least-once). No-op.
        db.rollback()
    except Exception:
        log.exception("n8n_handle_failed event=%s", getattr(evt, "name", "?"))
        db.rollback()
    finally:
        db.close()
