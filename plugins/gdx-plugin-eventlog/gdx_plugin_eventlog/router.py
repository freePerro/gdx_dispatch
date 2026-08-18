"""Read API for the event log. Mounted by plugin-host under /api/plugins/eventlog.
Scopes to the forwarded tenant; no module gate so the example is reachable
without a manually-inserted grant (see plugin_permissions notes)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import PluginContext, get_plugin_context, get_plugin_db
from gdx_plugin_eventlog.models import EventLogEntry

router = APIRouter()


@router.get("/events")
def list_events(
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    rows = (
        db.execute(
            select(EventLogEntry)
            .where(EventLogEntry.company_id == ctx.tenant_id)
            .order_by(EventLogEntry.received_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "event": r.event_name,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "delivery_id": r.delivery_id,
                "data": json.loads(r.payload_json) if r.payload_json else {},
            }
            for r in rows
        ]
    }
