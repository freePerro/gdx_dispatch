from __future__ import annotations

import importlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _import_twilio_client() -> Any:
    twilio_rest = importlib.import_module("twilio.rest")
    return twilio_rest.Client


def _configured() -> bool:
    return all(
        [
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN"),
            os.getenv("TWILIO_PHONE_NUMBER"),
        ]
    )


def send_sms(to_phone: str, body: str, from_phone: str, tenant_id: str) -> dict[str, Any]:
    if not _configured():
        logger.warning("sms not configured tenant_id=%s", tenant_id)
        return {"sent": False, "reason": "not configured"}

    client_cls = _import_twilio_client()
    client = client_cls(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    message = client.messages.create(to=to_phone, from_=from_phone, body=body)

    logger.info(
        "sms sent tenant_id=%s to=%s sid=%s",
        tenant_id,
        to_phone,
        getattr(message, "sid", ""),
    )

    return {
        "sent": True,
        "provider": "twilio",
        "message_id": getattr(message, "sid", None),
        "status": getattr(message, "status", None),
    }
