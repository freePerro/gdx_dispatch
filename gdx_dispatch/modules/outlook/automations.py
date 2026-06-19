"""Sprint Outlook Integration — Phase 7 auto-email triggers.

Per-user, opt-in: when a domain event fires (``invoice.created``,
``job.completed``, ``estimate.sent``), this module looks up enabled rules,
renders a tenant-managed template against the event context, and sends the
email AS the originating user via the existing ``send_mail`` Graph wrapper.

Configuration lives on ``OutlookSettings.auto_email_triggers``:

    {
      "invoice.created": {
        "enabled_default": False,
        "template": "Hi {{customer.name}}, your invoice {{invoice.number}} ...",
      },
      ...
    }

Per-user opt-in lives on the tenant-plane ``users.preferences`` JSON (existing
column) under key ``outlook_auto_email_opt_in.{trigger}``.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookSettings
from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired, with_outlook_client


log = logging.getLogger("gdx_dispatch.modules.outlook.automations")


_MUSTACHE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def render_template(template: str, context: dict[str, Any]) -> str:
    """Tiny mustache-style renderer. Supports dotted paths like {{customer.name}}.

    Missing keys render as the empty string. No escaping (HTML email body
    composers should pre-escape values that need it).
    """
    def _resolve(path: str) -> str:
        parts = path.split(".")
        current: Any = context
        for p in parts:
            if isinstance(current, dict):
                current = current.get(p)
            else:
                current = getattr(current, p, None)
            if current is None:
                return ""
        return str(current)

    return _MUSTACHE_RE.sub(lambda m: _resolve(m.group(1)), template)


def _user_opt_in(user, trigger: str) -> bool:
    """Read users.preferences['outlook_auto_email_opt_in'][trigger]."""
    prefs = getattr(user, "preferences", None) or {}
    if not isinstance(prefs, dict):
        return False
    opt_ins = prefs.get("outlook_auto_email_opt_in") or {}
    if not isinstance(opt_ins, dict):
        return False
    return bool(opt_ins.get(trigger))


def dispatch_trigger(
    trigger_name: str,
    context: dict[str, Any],
    *,
    user_id: UUID,
    tenant_id: UUID,
    tenant_db: Session,
    control_db: Session,
) -> dict[str, Any]:
    """Try to fire one auto-email for ``trigger_name``. Returns a small dict
    describing what happened (sent / skipped / error).

    The caller MUST resolve ``user_id`` (the user whose mailbox sends the
    email) and ``tenant_id`` from the event context. The user must have an
    OutlookAccount + opt-in for this trigger; otherwise → skipped.
    """
    settings = tenant_db.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    triggers = (settings.auto_email_triggers if settings else None) or {}
    rule = triggers.get(trigger_name)
    if not rule:
        return {"sent": False, "skipped": "trigger not configured"}
    template = rule.get("template")
    if not template:
        return {"sent": False, "skipped": "trigger has no template"}

    # Resolve user (for opt-in + outlook account presence)
    from gdx_dispatch.models.tenant_models import User
    # User.id is String(36) in tenant_models — coerce to avoid PG `text=uuid`
    user = tenant_db.get(User, str(user_id))
    if user is None:
        return {"sent": False, "skipped": "user not found"}

    if not _user_opt_in(user, trigger_name):
        return {"sent": False, "skipped": "user not opted in"}

    account = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .one_or_none()
    )
    if account is None or not account.access_token_enc:
        return {"sent": False, "skipped": "user has not connected outlook"}

    # Resolve recipient — context["customer"]["email"] is the convention.
    recipient = ((context.get("customer") or {}).get("email")
                 or (context.get("recipient_email")))
    if not recipient:
        return {"sent": False, "skipped": "no recipient resolved from context"}

    body_html = render_template(template, context)
    subject = render_template(rule.get("subject", trigger_name), context)

    # Send via Graph using the existing send-flow primitives directly to avoid
    # a self-HTTP roundtrip.
    body = {
        "message": {
            "subject": subject,
            "body": {"contentType": "html", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        },
        "saveToSentItems": True,
    }
    try:
        with with_outlook_client(control_db, tenant_db, user_id, tenant_id) as gc:
            gc._request("POST", "/me/sendMail", json=body)
    except (OutlookGraphAPIError, OutlookReconnectRequired) as exc:
        log.warning("auto-email %s failed for user %s: %s", trigger_name, user_id, exc)
        return {"sent": False, "error": str(exc)[:200]}

    log.info("auto-email %s sent for user %s", trigger_name, user_id)
    return {"sent": True, "recipient": recipient, "trigger": trigger_name}
