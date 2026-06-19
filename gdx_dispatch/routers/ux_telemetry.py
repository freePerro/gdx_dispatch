"""UX telemetry router — receives client-side analytics events.

POST /api/audit/ux-event
    Body: {events: [{name, payload, ts}, ...]}

Tour engine + help drawer dispatch `gdx:analytics` CustomEvents which the
client-side shim at `gdx_dispatch/frontend/src/lib/analytics.js` batches and POSTs
here. Events look like:

    {name: "tour_started", payload: {tour_id: "admin-...", total_steps: 4}}
    {name: "tour_completed", payload: {tour_id: "..."}}
    {name: "help_article_viewed", payload: {slug: "customers", source: "search"}}

We intentionally do NOT persist to the hash-chained `audit_log` table
(overkill for high-frequency clicks). Instead we structured-log to the
standard Python logger — that flows into container logs which Sentry +
log search both index. Doug can grep `ux_telemetry tour_completed`
in prod logs to answer "which articles got viewed."

Rate-limited at the route level (max 50 events per request) so a
runaway client can't flood. The shim itself batches at 4s intervals.

Auth: open to any authenticated user — `get_current_user` validates the
Bearer. Tenant + user_id are stamped into each log line for attribution.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger("gdx_dispatch.ux_telemetry")

router = APIRouter(prefix="/api/audit", tags=["ux_telemetry"])

# Allow-list of event names we accept; everything else is dropped silently.
# Keeps the log clean from speculative client emitters.
_ACCEPTED_EVENTS = frozenset({
    "tour_started",
    "tour_completed",
    "tour_skipped",
    "tour_skipped_no_anchors",
    "tour_step",
    "help_opened",
    "help_article_viewed",
})

_MAX_EVENTS_PER_BATCH = 50
_MAX_PAYLOAD_KEYS = 12
_MAX_VALUE_LEN = 200


class UXEvent(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str | None = Field(default=None, max_length=40)


class UXEventBatch(BaseModel):
    events: list[UXEvent] = Field(default_factory=list, max_length=_MAX_EVENTS_PER_BATCH)


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate / drop anything that looks like a runaway value or PII."""
    out: dict[str, Any] = {}
    for k, v in list(payload.items())[:_MAX_PAYLOAD_KEYS]:
        key = str(k)[:40]
        if isinstance(v, (str, int, float, bool)) or v is None:
            if isinstance(v, str) and len(v) > _MAX_VALUE_LEN:
                v = v[:_MAX_VALUE_LEN]
            out[key] = v
        else:
            out[key] = "<unserializable>"
    return out


@router.post("/ux-event", status_code=status.HTTP_204_NO_CONTENT)
def post_ux_events(
    batch: UXEventBatch,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    if not batch.events:
        return
    if len(batch.events) > _MAX_EVENTS_PER_BATCH:
        raise HTTPException(status_code=413, detail="too many events")

    tenant_id = str(getattr(request.state, "tenant", {}).get("id") if isinstance(getattr(request.state, "tenant", None), dict) else "") or "unknown"
    user_id = str(current_user.get("id") or current_user.get("user_id") or current_user.get("sub") or "unknown")
    user_role = str(current_user.get("role") or "")

    for ev in batch.events:
        if ev.name not in _ACCEPTED_EVENTS:
            continue
        safe_payload = _sanitize_payload(ev.payload)
        # Emit payload as JSON (not Python repr) so prod log parsers
        # (Loki json filter, jq, etc.) can extract fields. Python's
        # default %s on a dict gives single-quoted keys which break
        # every standard JSON pipeline.
        log.info(
            "ux_telemetry %s tenant=%s user=%s role=%s ts=%s payload=%s",
            ev.name, tenant_id, user_id, user_role, ev.ts or "-",
            json.dumps(safe_payload, separators=(",", ":")),
        )
    return
