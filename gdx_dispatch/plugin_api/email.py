"""Plugin email — queue mail for the core app to send (email overhaul P6).

The plugin-host container has NO internet egress by design, so a plugin can
never deliver mail itself — importing an SMTP/Graph client just dies at the
network wall, silently. This module is the sanctioned path: insert one
``plugin_email_outbox`` row on the shared DB (the same interface plugins use
for everything), and the core app's drain task delivers it through the
unified pipeline — Outlook-or-SMTP as the tenant's designated automation
sender, branded shell for ``body_text`` / raw markup for ``body_html``, and
an ``outbound_emails`` audit row (initiator ``plugin/<key>``) either way.

Requirements:
- The plugin must declare the ``"email"`` permission in its manifest and the
  owner must have consented (ADR-014). Enforced at DRAIN time by core —
  a queued row from an unconsented plugin fails with ``consent_missing``.
- ``delivery_id`` is the idempotency key, scoped PER PLUGIN: queueing is
  at-least-once, sending is exactly-once per (plugin_key, delivery_id).
  Re-queue with the same key = no-op.

Usage from a plugin::

    from gdx_dispatch.plugin_api.email import queue_email

    queue_email(
        db, tenant_id=ctx.tenant_id, plugin_key="myplugin",
        delivery_id=f"welcome:{customer_id}",
        subject="Welcome!", body_text="Hi {name}, ...",
        customer_id=customer_id,
    )
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def queue_email(
    db: Session,
    *,
    tenant_id: str,
    plugin_key: str,
    delivery_id: str,
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    to_email: str | None = None,
    customer_id: str | None = None,
    contact_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Queue one email. Returns {queued, id?, reason?}.

    Exactly one of ``body_text`` (rendered in the branded shell) or
    ``body_html`` (sent raw — full access) is required, and at least one of
    ``to_email`` / ``customer_id`` (customer routes through the person-aware
    recipient resolver; ``contact_id`` pins a specific person).
    Never raises on a duplicate delivery_id — that is the idempotent no-op.
    """
    from sqlalchemy.exc import IntegrityError

    from gdx_dispatch.models.tenant_models import PluginEmailOutbox

    if not (subject or "").strip():
        return {"queued": False, "reason": "empty_subject"}
    if bool(body_text) == bool(body_html):
        return {"queued": False, "reason": "exactly_one_of_body_text_or_body_html"}
    if not (to_email or customer_id):
        return {"queued": False, "reason": "no_recipient"}
    if not (delivery_id or "").strip():
        return {"queued": False, "reason": "empty_delivery_id"}

    row = PluginEmailOutbox(
        company_id=str(tenant_id or ""),
        plugin_key=str(plugin_key or ""),
        delivery_id=str(delivery_id)[:80],
        to_email=to_email,
        customer_id=str(customer_id) if customer_id else None,
        contact_id=str(contact_id) if contact_id else None,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        log.info("plugin_email_duplicate_delivery_id plugin=%s id=%s", plugin_key, delivery_id)
        return {"queued": False, "reason": "duplicate_delivery_id"}
    return {"queued": True, "id": str(row.id)}
