"""Read API for the n8n Automations console. Mounted by plugin-host under
/api/plugins/n8n. Scopes to the forwarded tenant.

Every `type: list` screen endpoint returns a BARE ARRAY of row objects — the host
list renderer assigns the response straight to the DataTable value with no
envelope unwrap, so a wrapper would render zero rows (see
docs + plugin-list-endpoint contract).
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import PluginContext, get_plugin_context, get_plugin_db
from gdx_plugin_n8n.catalog import EVENT_CATALOG
from gdx_plugin_n8n.models import N8nEvent


router = APIRouter()


def _payload_summary(payload_json: str | None) -> str:
    """A one-glance description of an event row for the Activity table — the
    human name or id inside the payload, never the whole blob."""
    if not payload_json:
        return ""
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("name", "invoice_number", "customer_id", "invoice_id", "job_id", "id"):
        val = data.get(key)
        if val:
            return f"{key}={val}"
    return ""


def _n8n_editor_url() -> str:
    """The customer's own n8n editor address, for the Connect screen. Read from
    the environment the operator set at deploy; falls back to guidance (never a
    fabricated URL) so the screen is honest on a box where it isn't set yet."""
    url = (os.getenv("GDX_N8N_URL") or "").strip()
    if url:
        return url
    host = (os.getenv("N8N_HOST") or "").strip()
    if host:
        return f"https://{host}"
    return "Not set — it's your own n8n instance (e.g. https://n8n.your-domain.com)"


@router.get("/events")
def list_events(
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    """Live activity: business events the automation layer has seen for this
    tenant, newest first. This is what n8n workflows can trigger on, as it
    happens."""
    rows = (
        db.execute(
            select(N8nEvent)
            .where(N8nEvent.company_id == ctx.tenant_id)
            .order_by(N8nEvent.received_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    return [
        {
            "event": r.event_name,
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "detail": _payload_summary(r.payload_json),
        }
        for r in rows
    ]


@router.get("/catalog")
def list_catalog(
    ctx: PluginContext = Depends(get_plugin_context),
) -> list[dict]:
    """The GDX events a workflow can subscribe to (bare array)."""
    return [{"event": name, "fires_when": desc} for name, desc in EVENT_CATALOG]


@router.get("/connect")
def connection_info(
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    """Dynamic setup facts for the Connect screen (bare array of {setting,value}).

    The static Setup tab explains the model; this surfaces the live values a
    static help screen can't — the editor URL and how many events GDX has
    emitted so far. Counts, not row loads (COUNT(*), not len of a full select)."""
    seen = db.execute(
        select(func.count()).select_from(N8nEvent).where(
            N8nEvent.company_id == ctx.tenant_id
        )
    ).scalar() or 0
    return [
        {"setting": "Your n8n editor", "value": _n8n_editor_url()},
        {
            "setting": "Send events to n8n",
            "value": "Add a webhook in Settings → Webhooks pointing at your n8n "
            "Webhook node's URL. That webhook — not this console — delivers events "
            "to n8n; GDX signs and POSTs each one.",
        },
        {
            "setting": "Confirm delivery",
            "value": "Delivery successes, retries and failures to n8n show in "
            "Settings → Webhooks. The Activity tab here shows what GDX emitted, "
            "not what n8n received.",
        },
        {
            "setting": "Verifying in n8n",
            "value": "GDX signs each delivery with the secret you set on that "
            "webhook; verify the X-GDX-Signature header in your n8n workflow.",
        },
        {
            "setting": "Events in the catalog",
            "value": f"{len(EVENT_CATALOG)} event types — see Available Events. "
            "Not all emit in every release; Activity shows what's live.",
        },
        {
            "setting": "Events emitted so far",
            "value": f"{seen} event{'' if seen == 1 else 's'} seen by this console "
            "— see the Activity tab.",
        },
    ]
