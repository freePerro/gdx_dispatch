"""Cell-gateway webhook — the shared-secret door for the personal cell's feed.

The android-nomad-gateway app on the owner's phone POSTs each incoming text /
call here (reasoning recorded in the cell-comms nomad-gateway design record).
This shim exists because plugin routes are reachable only through the
authenticated core proxy and there is no device-token surface (PATs were
deleted 2026-08-12) — so core authenticates the phone by shared secret
(core/webhook_auth.py, the inbound-email gate's policy) and relays the payload
to the cellcomms plugin, which owns parsing, storage, matching and screens.

Like the inbound-email webhook, the company is never taken from the request —
single-tenant, `company_id()` decides. The relay stamps the same forwarded
headers the plugin proxy uses, with a synthetic "cell-gateway" actor: the
plugin trusts those headers only because both callers (proxy, this shim) are
core-controlled.

No message body or phone number goes to logs or audit details — texts are PII.
The audit row records that an event of a kind arrived and where it landed.
"""
from __future__ import annotations

import contextlib
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import get_company_id
from gdx_dispatch.core.webhook_auth import verify_shared_secret
from gdx_dispatch.plugin_api.context import H_MODULES, H_ROLE, H_TENANT, H_USER

log = logging.getLogger(__name__)

# noqa S105: names of the header and env var, not a secret.
SECRET_HEADER = "X-GDX-Cell-Secret"  # noqa: S105
SECRET_ENV = "CELL_GATEWAY_WEBHOOK_SECRET"  # noqa: S105

# One SMS or one call event. 64 KB allows a max multipart SMS several times
# over; anything bigger is not a phone event.
MAX_BODY_BYTES = 64 * 1024

PLUGIN_KEY = "cellcomms"

public_router = APIRouter(tags=["cell_gateway_public"])


async def verify_cell_secret(request: Request) -> None:
    verify_shared_secret(request, header=SECRET_HEADER, secret_env=SECRET_ENV)


def _plugin_host_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


def _client() -> httpx.AsyncClient:
    """Factory, module-level so tests can swap in an ASGI-transport client and
    drive the REAL plugin router instead of a mock."""
    return httpx.AsyncClient(timeout=15.0)


@public_router.post("/api/cell-gateway/webhook", response_model=None)
async def cell_gateway_webhook(
    request: Request,
    _secret: None = Depends(verify_cell_secret),
    tenant_id: str = Depends(get_company_id),
    db: Session = Depends(get_db),
) -> dict:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="event too large")
    if not body.strip():
        raise HTTPException(status_code=422, detail="empty body")

    fwd = {
        "content-type": request.headers.get("content-type", "application/json"),
        H_TENANT: tenant_id,
        H_USER: "cell-gateway",
        H_ROLE: "webhook",
        H_MODULES: "",
    }
    url = f"{_plugin_host_url()}/api/plugins/{PLUGIN_KEY}/ingest"
    try:
        async with _client() as client:
            upstream = await client.post(url, content=body, headers=fwd)
    except httpx.HTTPError as exc:
        # Return 502 so nomad's retry-with-backoff redelivers once the
        # plugin-host is back — a 200 here would silently drop the event.
        log.warning("cell_gateway_relay_failed err=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="plugin-host unreachable") from exc

    if upstream.status_code >= 500:
        raise HTTPException(status_code=502, detail="plugin ingest failed")

    result: dict = {}
    with contextlib.suppress(ValueError):
        result = upstream.json()

    if upstream.status_code == 200 and result.get("status") == "ok":
        # Guarded like inbound_comms._audit: the event already landed in the
        # plugin table — an audit hiccup must not make nomad redeliver it.
        try:
            log_audit_event_sync(
                db,
                tenant_id=tenant_id,
                user_id="cell-gateway",
                action="cell_event_received",
                entity_type=f"cell_{result.get('kind', 'event')}",
                entity_id=str(result.get("id", "")),
                details={"kind": result.get("kind"), "source": "nomad-gateway"},
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("cell_gateway_audit_failed kind=%s", result.get("kind"))
            db.rollback()

    # Pass the plugin's verdict through (200 duplicate = cheap no-op for
    # nomad's at-least-once retries; 422 = misconfigured template on the phone).
    if upstream.status_code != 200:
        raise HTTPException(status_code=upstream.status_code, detail=result.get("detail", "ingest rejected"))
    return result
