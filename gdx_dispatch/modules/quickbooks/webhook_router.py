from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request as FastAPIRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.quickbooks.webhook_models import QBWebhookEvent

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/qb", tags=["quickbooks"], dependencies=[Depends(require_module("quickbooks"))])


def _verify_signature(raw_body: bytes, sig_header: str, verifier_token: str) -> bool:
    """S122-5 (C3): HMAC-SHA256 verification against raw bytes using the
    ``intuit-signature`` header (per Intuit Developer docs). The previous
    handler did a literal-string compare with the wrong header name and the
    wrong algorithm — real Intuit deliveries 403'd, forged events passed.
    """
    if not verifier_token or not sig_header:
        return False
    expected = base64.b64encode(
        hmac.new(verifier_token.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, sig_header)


def _normalize_old_format(payload: dict) -> list[tuple[str, str, str, str]]:
    """Old format: {eventNotifications: [{realmId, dataChangeEvent: {entities: [...]}}]}
    Returns list of (realm_id, entity_name, entity_id, operation).
    """
    out: list[tuple[str, str, str, str]] = []
    for notification in payload.get("eventNotifications", []) or []:
        realm_id = str(notification.get("realmId") or "")
        entities = (notification.get("dataChangeEvent") or {}).get("entities") or []
        for entity in entities:
            ename = str(entity.get("name") or "")
            eid = str(entity.get("id") or "")
            op = str(entity.get("operation") or "")
            if realm_id and ename and eid and op:
                out.append((realm_id, ename, eid, op))
    return out


# QBO entity names use CamelCase that the lowercased CloudEvents `type` field
# doesn't preserve — ``qbo.salesreceipt.created.v1`` must round-trip to
# ``SalesReceipt`` not ``Salesreceipt``. Single-word entities are reproduced
# by str.capitalize(); the compound names below need the explicit map.
_QBO_ENTITY_CANONICAL: dict[str, str] = {
    "billpayment": "BillPayment",
    "creditmemo": "CreditMemo",
    "journalentry": "JournalEntry",
    "refundreceipt": "RefundReceipt",
    "salesreceipt": "SalesReceipt",
    "timeactivity": "TimeActivity",
    "taxagency": "TaxAgency",
    "companyinfo": "CompanyInfo",
    "preferences": "Preferences",
}


def _canonicalize_entity_name(lower: str) -> str:
    return _QBO_ENTITY_CANONICAL.get(lower, lower.capitalize())


def _normalize_cloudevents_format(payload: list) -> list[tuple[str, str, str, str]]:
    """S122-CE (B1): CloudEvents v1.0 format mandatory by Intuit 2026-07-31.
    Top-level is an array; each event has ``type`` like ``qbo.invoice.created.v1``.
    Returns list of (realm_id, entity_name, entity_id, operation).
    """
    out: list[tuple[str, str, str, str]] = []
    for event in payload or []:
        if not isinstance(event, dict):
            continue
        realm_id = str(event.get("intuitaccountid") or "")
        entity_id = str(event.get("intuitentityid") or "")
        event_type = str(event.get("type") or "")
        # type is "qbo.<entity>.<operation>.v1" — e.g. "qbo.invoice.created.v1"
        parts = event_type.split(".")
        if len(parts) < 3 or parts[0] != "qbo":
            continue
        entity_name = _canonicalize_entity_name(parts[1].lower())
        operation = parts[2].capitalize()    # created → Created, updated → Updated, deleted → Deleted
        if realm_id and entity_name and entity_id and operation:
            out.append((realm_id, entity_name, entity_id, operation))
    return out


@router.post("/webhook")
async def qb_webhook(
    request: FastAPIRequest,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Receive QuickBooks change-notification webhooks.

    Accepts both the legacy ``{eventNotifications: [...]}`` format and the
    new CloudEvents v1.0 array format (mandatory by 2026-07-31 per
    https://blogs.intuit.com/2025/11/12/upcoming-change-to-webhooks-payload-structure/).

    Signature verification is HMAC-SHA256 against the raw request bytes,
    using the ``intuit-signature`` header and the ``QB_WEBHOOK_VERIFIER_TOKEN``
    env var as the secret. Compared with ``hmac.compare_digest`` to avoid
    timing side channels.

    Events are deduplicated by ``{realm_id}:{entity_name}:{entity_id}:{operation}``
    in ``qb_webhook_events``. New Customer/Invoice events queue a sync task;
    other entity types are recorded but not dispatched yet (see D-S122-push-coverage-gap
    + N5 per-entity task wiring in Phase 2).
    """
    # S122-CE auditor catch round 2: cap raw body size on an unauthenticated
    # surface. Intuit's typical webhook payload is a few KB; cap at 1 MB —
    # anything larger is malicious or malformed.
    raw_body = await request.body()
    if len(raw_body) > 1_048_576:
        raise HTTPException(status_code=413, detail="Webhook payload too large")

    verifier_token = os.getenv("QB_WEBHOOK_VERIFIER_TOKEN") or os.getenv("QB_WEBHOOK_SECRET", "")
    if verifier_token:
        sig_header = request.headers.get("intuit-signature", "")
        if not _verify_signature(raw_body, sig_header, verifier_token):
            raise HTTPException(status_code=403, detail="Invalid QuickBooks webhook signature")

    try:
        payload: Any = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.exception("qb_webhook_invalid_json")
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    if isinstance(payload, list):
        events = _normalize_cloudevents_format(payload)
        fmt = "cloudevents"
    elif isinstance(payload, dict):
        events = _normalize_old_format(payload)
        fmt = "legacy"
    else:
        raise HTTPException(status_code=400, detail="Unexpected payload shape")

    tenant_id: str = str(company_id())
    processed = 0
    skipped = 0
    unhandled = 0
    suppressed_ledger_on = 0

    # GL S9 (spec §5.4): with ledger posting on, Invoice/Payment webhooks must
    # not enqueue pull tasks — every GDX→QBO push echoes back as a webhook, so
    # dispatching would mean a permanently-failing task per push. Suppress at
    # dispatch (events are still recorded + audited below); the in-pull gate
    # remains as the loud backstop for direct enqueues.
    from gdx_dispatch.modules.quickbooks.sync import money_pulls_disabled as _pulls_off  # noqa: PLC0415

    suppress_money_dispatch = _pulls_off(db, tenant_id)

    for realm_id, entity_name, entity_id, operation in events:
        event_id = f"{realm_id}:{entity_name}:{entity_id}:{operation}"

        existing = db.execute(
            select(QBWebhookEvent).where(QBWebhookEvent.event_id == event_id)
        ).scalar_one_or_none()
        if existing is not None:
            skipped += 1
            continue

        evt = QBWebhookEvent(
            event_id=event_id, event_type=entity_name, entity_id=entity_id, realm_id=realm_id,
        )
        db.add(evt)
        db.flush()

        await log_audit_event(
            db, "qb_webhook_received", "system", "qb_webhook", event_id,
            {"tenant_id": tenant_id, "realm_id": realm_id, "entity_name": entity_name,
             "entity_id": entity_id, "operation": operation, "format": fmt},
        )
        db.commit()

        # S122-11 + S122-12 (2026-05-13): per-entity dispatch. Webhook events
        # mean "QB side changed" so the response is PULL FROM QB. Pre-fix
        # Customer/Invoice routed to sync_all_*_task which PUSHES every GDX row
        # to QB (wrong direction); Payment/Item/Vendor/Account/JournalEntry
        # were silently dropped (N5).
        #
        # JournalEntry has no pull_* in sync.py; logged as unhandled (filed
        # D-S122-12-journalentry-pull).
        _PER_ENTITY_TASK_BY_NAME = {
            "Customer": "sync_customer_task",
            "Invoice": "sync_invoice_task",
            "Payment": "sync_payment_task",
            "Item": "sync_item_task",
            "Vendor": "sync_vendor_task",
            "Account": "sync_account_task",
        }
        task_name = _PER_ENTITY_TASK_BY_NAME.get(entity_name)
        if task_name and suppress_money_dispatch and entity_name in ("Invoice", "Payment"):
            log.warning(
                "qb_webhook_money_pull_suppressed tenant=%s entity=%s op=%s — "
                "ledger_posting_enabled: GDX is the book of record (GL spec §5.4)",
                tenant_id, entity_name, operation,
            )
            suppressed_ledger_on += 1
        elif task_name:
            from gdx_dispatch.modules.quickbooks import tasks as qb_tasks  # noqa: PLC0415
            task = getattr(qb_tasks, task_name)
            task.delay(tenant_id, entity_id)
            processed += 1
        else:
            log.info(
                "qb_webhook_unhandled_entity tenant=%s entity=%s op=%s realm=%s",
                tenant_id, entity_name, operation, realm_id,
            )
            unhandled += 1

    return {
        "processed": processed,
        "skipped": skipped,
        "unhandled": unhandled,
        "suppressed_ledger_on": suppressed_ledger_on,
        "format_seen": 1,
    }
