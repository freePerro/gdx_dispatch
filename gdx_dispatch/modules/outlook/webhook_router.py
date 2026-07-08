"""Sprint Outlook Integration — Microsoft Graph webhook receiver.

Path: ``GET/POST /api/webhooks/outlook/{tenant_slug}/{client_state}``.

Two flows:

1. **Validation handshake**: Microsoft **POSTs** the URL with
   ``?validationToken=...`` and an EMPTY body once on subscription create;
   we must echo the token back as text/plain 200 within 10 seconds.
   (2026-07-08 prod catch: this doc used to say Graph GETs — only the GET
   route echoed the token, the POST route tried ``request.json()`` on the
   empty body and 400'd, so every subscription create failed Graph-side
   validation. The GET echo is kept for manual probing.)

2. **Change notifications**: subsequent POSTs carry ``{"value": [...]}``. Each
   event has its own ``clientState``. We verify path secret + payload clientState
   match the per-subscription row, then enqueue a Celery sync task. Returns 202
   quickly so MS doesn't retry-storm us.
"""
from __future__ import annotations

import contextlib
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.database import SessionLocal, SessionLocal
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookSubscription


log = logging.getLogger("gdx_dispatch.modules.outlook.webhook_router")

router = APIRouter(
    prefix="/api/webhooks/outlook",
    tags=["webhooks", "outlook"],
)


def _open_tenant_session(tenant_id: UUID) -> Session:
    return SessionLocal()


@router.get("/{tenant_slug}/{client_state}")
def validate_handshake(
    tenant_slug: str,
    client_state: str,
    validationToken: str | None = Query(None),  # noqa: N803 — MS spelling
) -> PlainTextResponse:
    """Microsoft validation handshake. Returns the token verbatim as text/plain."""
    if not validationToken:
        raise HTTPException(status_code=400, detail="missing validationToken")
    log.info("outlook validation handshake: tenant=%s", tenant_slug)
    return PlainTextResponse(validationToken, status_code=200)


@router.post("/{tenant_slug}/{client_state}")
async def receive_notifications(
    tenant_slug: str,
    client_state: str,
    request: Request,
    validationToken: str | None = Query(None),  # noqa: N803 — MS spelling
) -> Response:
    """Accept change notifications. Verify clientState, enqueue, return 202."""
    if validationToken:
        # Graph's subscription-validation handshake is a POST with an empty
        # body — echo the token before any JSON parsing or the create fails
        # with "Notification endpoint must respond with 200 OK".
        log.info("outlook validation handshake (POST): tenant=%s", tenant_slug)
        return PlainTextResponse(validationToken, status_code=200)
    body = await request.json()
    events = body.get("value") or []
    if not events:
        return Response(status_code=202)

    with contextlib.closing(SessionLocal()) as cdb:
        # Case-insensitive slug lookup — tolerates URL-construction drift.
        from sqlalchemy import func
        tenant = (
            cdb.query(Tenant)
            .filter(func.lower(Tenant.slug) == tenant_slug.lower())
            .one_or_none()
        )
        if tenant is None:
            log.warning("outlook webhook: unknown tenant slug %s", tenant_slug)
            raise HTTPException(status_code=404, detail="unknown tenant")
        tenant_id = tenant.id

    enqueued = 0
    with contextlib.closing(_open_tenant_session(tenant_id)) as tdb:
        for ev in events:
            ev_client_state = ev.get("clientState")
            sub_id = ev.get("subscriptionId")
            if not ev_client_state or ev_client_state != client_state:
                log.warning("outlook webhook: clientState mismatch for sub %s", sub_id)
                continue
            sub = (
                tdb.query(OutlookSubscription)
                .filter(OutlookSubscription.client_state == client_state)
                .one_or_none()
            )
            if sub is None:
                log.warning("outlook webhook: no OutlookSubscription for client_state")
                continue
            account = tdb.get(OutlookAccount, sub.account_id)
            if account is None:
                continue
            _enqueue_sync(account.id, tenant_id)
            enqueued += 1
    log.info("outlook webhook: enqueued %d sync(s) for tenant %s", enqueued, tenant_slug)
    return Response(status_code=202)


def _enqueue_sync(account_id: UUID, tenant_id: UUID) -> None:
    """Lazy import so test envs without Celery don't fail at import-time.

    Enqueue failures are loud: webhook returns 202 (Microsoft must not retry-
    storm us), but we log.exception so an outage is visible in the log
    aggregator. The fallback poller (15-min cadence) will catch up the sync
    if Celery comes back online.
    """
    try:
        from gdx_dispatch.modules.outlook.tasks import sync_outlook_mailbox
        sync_outlook_mailbox.delay(str(account_id), str(tenant_id))
    except Exception:  # noqa: BLE001
        log.exception(
            "outlook webhook: Celery enqueue failed for account=%s tenant=%s — "
            "fallback poller will retry within 15 min",
            account_id, tenant_id,
        )
