"""Drain the plugin email outbox (email overhaul Phase 6).

Plugins queue rows via plugin_api.email.queue_email (plugin-host has no
egress); this beat task delivers them through the unified pipeline. Core
owns every decision the plugin can't be trusted with:

- CONSENT at drain time (ADR-014): the plugin must hold the owner-consented
  "email" permission NOW, not just at queue time — an uninstalled or
  consent-revoked plugin's queued mail fails with consent_missing.
- Identity: sends go out as the tenant's automation sender (the same
  app_settings.automation_sender_user_id workflow rules use), falling back
  to SMTP. NOT gated on automation_emails_enabled — that toggle governs the
  workflow engine; plugin email is governed by its own install consent.
- Audit: send_transactional_email writes the outbound_emails row
  (initiator plugin/<key>, kind 'plugin') for every attempt.

Retry: transient failures re-queue up to MAX_ATTEMPTS; permanent reasons
(consent, recipient, body) fail immediately. One row's failure never blocks
the batch.
"""
from __future__ import annotations

import logging
from typing import Any

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)

BATCH_SIZE = 25
MAX_ATTEMPTS = 3
# Failures that retrying cannot fix.
_PERMANENT = {
    "consent_missing",
    "no_recipient_email",
    "no_recipient",
    "customer_not_found",
    "empty_body",
    "no_recipient_email_or_customer",
}


def _automation_sender(db) -> str | None:
    try:
        from gdx_dispatch.models.tenant_models import AppSettings

        row = db.query(AppSettings).first()
        return getattr(row, "automation_sender_user_id", None) or None if row else None
    except Exception:
        log.exception("plugin_email_sender_read_failed")
        return None


def _consented(db, plugin_key: str) -> bool:
    try:
        from gdx_dispatch.core.plugin_consent import consented_permissions

        return "email" in consented_permissions(db, plugin_key)
    except Exception:
        # Fail closed: no consent proof = no send.
        log.exception("plugin_email_consent_check_failed plugin=%s", plugin_key)
        return False


def _deliver(db, row) -> tuple[bool, str | None]:
    from gdx_dispatch.core.email_layout import (
        email_branding,
        linkify,
        nl2br,
        render_email,
    )
    from gdx_dispatch.core.email_recipients import resolve_recipient
    from gdx_dispatch.core.transactional_email import send_transactional_email
    from gdx_dispatch.models.tenant_models import Customer

    if not _consented(db, row.plugin_key):
        return False, "consent_missing"

    branding = email_branding(db)
    to_email = (row.to_email or "").strip()
    to_name = ""
    greeting = ""
    recipient_source = "override" if to_email else None
    recipient_contact_id = None
    if row.customer_id:
        from uuid import UUID as _UUID

        from sqlalchemy import select as _select

        try:
            customer = db.execute(
                _select(Customer).where(
                    Customer.id == _UUID(str(row.customer_id)),
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        except (ValueError, TypeError):
            customer = None
        if customer is None:
            return False, "customer_not_found"
        resolved = resolve_recipient(db, customer, row.contact_id)
        if not resolved.ok:
            return False, "no_recipient_email"
        to_email = resolved.email
        to_name = resolved.to_name
        greeting = resolved.greeting_name
        recipient_source = resolved.source
        recipient_contact_id = resolved.contact_id
    if not to_email:
        return False, "no_recipient_email"

    if row.body_html:
        html = row.body_html
    else:
        accent = branding.get("accent") or "#2563eb"
        text = (row.body_text or "").replace("{name}", greeting or to_name or "")
        if not text.strip():
            return False, "empty_body"
        body_html = (
            '<p style="margin:0 0 12px;">'
            + linkify(nl2br(text), accent).replace(
                "<br><br>", '</p><p style="margin:0 0 12px;">'
            )
            + "</p>"
        )
        html = render_email(
            branding=branding, body_html=body_html,
            title=row.subject, preheader=row.subject,
        )

    sent, _provider, skip_reason = send_transactional_email(
        tenant_db=db,
        tenant_id=str(row.company_id or ""),
        user_id=_automation_sender(db),
        to_email=to_email,
        to_name=to_name,
        subject=row.subject,
        html_body=html,
        initiator_kind="plugin",
        initiator_ref=row.plugin_key,
        kind="plugin",
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        recipient_source=recipient_source,
        recipient_contact_id=recipient_contact_id,
    )
    return sent, skip_reason


@celery_app.task
def drain_plugin_email_outbox() -> dict[str, Any]:
    """Process up to BATCH_SIZE queued rows. Returns counters."""
    from sqlalchemy import select

    from gdx_dispatch.core.audit import utcnow
    from gdx_dispatch.models.tenant_models import PluginEmailOutbox

    sent = failed = retried = 0
    with SessionLocal() as db:
        rows = db.execute(
            select(PluginEmailOutbox)
            .where(PluginEmailOutbox.status == "queued")
            .order_by(PluginEmailOutbox.created_at)
            .limit(BATCH_SIZE)
        ).scalars().all()
        for row in rows:
            try:
                ok, reason = _deliver(db, row)
            except Exception:
                log.exception("plugin_email_deliver_crashed id=%s", row.id)
                ok, reason = False, "exception"
            row.attempts = (row.attempts or 0) + 1
            row.processed_at = utcnow()
            if ok:
                row.status = "sent"
                row.last_error = None
                sent += 1
            elif reason in _PERMANENT or row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                row.last_error = (reason or "send_failed")[:120]
                failed += 1
            else:
                # stays queued for the next drain
                row.last_error = (reason or "send_failed")[:120]
                retried += 1
            db.commit()
    return {"sent": sent, "failed": failed, "retried": retried}
