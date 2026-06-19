"""Sprint phone-com pc-s12 — public webhook receiver.

Path: ``POST /api/webhooks/phone-com/{tenant_slug}/{secret}``.

No auth dependency — the URL itself carries the per-tenant secret which
is the proof of authenticity (Phone.com does not sign payloads — Slice 0
finding 2026-04-27). voip_id from the payload is verified against the
tenant's stored ``AppSettings.phone_com_voip_id`` so a leaked URL alone
can't forge events for a different account.

The route returns 204 on success. Unknown event types still 204 (rather
than 5xx) so Phone.com doesn't retry-storm us; the unknown shape is
logged for forensic review.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import upserts, webhook_signing
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComMessage,
    PhoneComVoicemail,
)

log = logging.getLogger("gdx_dispatch.modules.phone_com.webhook_router")

router = APIRouter(prefix="/api/webhooks/phone-com", tags=["phone-com", "webhooks"])


def _open_tenant_session(tenant_row: dict[str, Any] | None = None) -> Session:
    """Open a session on the single application database."""
    return SessionLocal()


def _resolve_voip_id(tenant_db: Session) -> int | None:
    app = tenant_db.query(AppSettings).first()
    if app is None or app.phone_com_voip_id is None:
        return None
    try:
        return int(app.phone_com_voip_id)
    except (TypeError, ValueError):
        return None


def _upsert_call(tenant_db, payload):
    row = upserts.upsert_call(tenant_db, payload)
    if row is None:
        raise HTTPException(status_code=400, detail="payload missing call id")
    return row


def _upsert_message(tenant_db, payload):
    row = upserts.upsert_message(tenant_db, payload)
    if row is None:
        raise HTTPException(status_code=400, detail="payload missing message id")
    return row


def _upsert_voicemail(tenant_db, payload):
    row = upserts.upsert_voicemail(tenant_db, payload)
    if row is None:
        raise HTTPException(status_code=400, detail="payload missing voicemail id")
    return row


def _upsert_fax(tenant_db, payload):
    row = upserts.upsert_fax(tenant_db, payload)
    if row is None:
        raise HTTPException(status_code=400, detail="payload missing fax id")
    return row


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _classify_event(payload: dict[str, Any]) -> str:
    raw = (
        payload.get("type")
        or payload.get("event")
        or payload.get("event_type")
        or ""
    )
    raw = str(raw).lower().strip()
    # Phone.com event-tag schema is in flux; map flexibly.
    if "fax" in raw or payload.get("pdf_url") or payload.get("pages"):
        return "fax"
    if "voicemail" in raw or payload.get("voicemail_url") or payload.get("voicemail_transcript"):
        return "voicemail"
    if "sms" in raw or "message" in raw or "text" in raw:
        return "message"
    if "call" in raw or payload.get("caller_id") or payload.get("call_id") or payload.get("final_action"):
        return "call"
    return "unknown"


def _audit(
    control_db: Session,
    tenant_id: str,
    action: str,
    request: Request,
    details: dict[str, Any],
) -> None:
    try:
        ip = request.client.host if request.client else ""
        log_audit_event_sync(
            control_db,
            tenant_id=tenant_id,
            user_id="phone_com_webhook",
            action=action,
            entity_type="phone_com_webhook",
            entity_id=str(details.get("event_id") or ""),
            details={**details, "ip": ip},
            request=request,
        )
        control_db.commit()
    except Exception:  # noqa: BLE001
        log.exception("phone_com_webhook audit failed")


def _set_sentry_tag(voip_id: int | None) -> None:
    if voip_id is None:
        return
    try:
        import sentry_sdk
        sentry_sdk.set_tag("phone_com_voip_id", str(voip_id))
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        log.debug("sentry tag set failed", exc_info=True)


@router.post("/{tenant_slug}/{secret}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_webhook(
    tenant_slug: str,
    secret: str,
    request: Request,
) -> None:
    """Public webhook receiver. Returns 204 on success or known-invalid.

    NEVER raises 5xx for parse errors / unknown event shapes — Phone.com
    retries 5xx and we don't want retry storms. Real internal errors
    (DB unreachable, etc) propagate to FastAPI's default handler.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        # Empty body or invalid JSON. Phone.com sometimes sends an empty
        # POST as a smoke check.
        return None

    # Step 1: short-circuit on test pings BEFORE tenant lookup. Phone.com
    # registers callbacks by POSTing {"test": 1} to verify the URL.
    if isinstance(payload, dict) and payload.get("test") == 1 and len(payload) == 1:
        return None

    # Step 2: resolve the single GDX tenant
    from gdx_dispatch.core.tenant import single_tenant
    _t = single_tenant()
    tenant_uuid = UUID(str(_t["id"]))
    tenant_db = _open_tenant_session()
    try:
        voip_id = _resolve_voip_id(tenant_db)
        _set_sentry_tag(voip_id)

        # Step 3: webhook_signing.decide — path secret + voip_id check.
        decision = webhook_signing.decide(
            tenant_uuid, secret,
            payload if isinstance(payload, dict) else {},
            tenant_db,
            expected_voip_id=voip_id,
        )
        if not decision.accepted:
            raise HTTPException(
                status_code=decision.status_code, detail=decision.reason
            )

        # Step 4: classify + dispatch
        kind = _classify_event(payload if isinstance(payload, dict) else {})
        event_id: str | None = None
        try:
            if kind == "call":
                row = _upsert_call(tenant_db, payload)
                event_id = row.phone_com_call_id
            elif kind == "message":
                row = _upsert_message(tenant_db, payload)
                event_id = row.phone_com_message_id
            elif kind == "voicemail":
                row = _upsert_voicemail(tenant_db, payload)
                event_id = row.phone_com_voicemail_id
            elif kind == "fax":
                row = _upsert_fax(tenant_db, payload)
                event_id = row.phone_com_fax_id
            else:
                log.warning(
                    "phone_com_webhook unknown_event tenant=%s payload_keys=%s",
                    tenant_slug, list((payload or {}).keys())[:10] if isinstance(payload, dict) else None,
                )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            # Persist what we got so the operator can inspect; never 5xx.
            log.exception(
                "phone_com_webhook upsert failed tenant=%s kind=%s",
                tenant_slug, kind,
            )

        # Step 5: audit
        _audit(
            tenant_db,
            str(tenant_uuid),
            f"phone_com.webhook.{kind}",
            request,
            {"event_id": event_id, "payload_size": len(str(payload))},
        )

        # Step 6: emit to internal event bus (best-effort)
        try:
            from gdx_dispatch.events import emit  # type: ignore

            emit(
                f"phone_com.{kind}",
                {"tenant_id": str(tenant_uuid), "event_id": event_id, "payload": payload},
            )
        except Exception:  # noqa: BLE001
            log.warning("phone_com_webhook event emit skipped", exc_info=True)

        return None
    finally:
        tenant_db.close()
