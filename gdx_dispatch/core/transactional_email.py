"""Unified transactional-email path for invoice / estimate / receipt sends.

Before S110, every transactional send (`send_estimate`, `send_invoice`,
`mobile_invoicing._send_invoice_email`) called `gdx_dispatch.core.email_sender.send_email`,
which reads tenant SMTP creds from the `email_settings` table. On any tenant
that hasn't configured SMTP — including GDX — the path silently logs
"Email not configured for tenant" and returns False, so the customer
never receives anything even though the UI flips status to "sent" and
records an audit row.

Meanwhile the same tenant has a fully-functional Outlook OAuth integration
(see `gdx_dispatch/modules/outlook/`) that can send through Microsoft Graph as the
calling user. The token plumbing, control-plane app registration, and per-
user `outlook_accounts` row are already present.

`send_transactional_email` is the new single entry point. It picks a
provider in this order:
1. Outlook Graph, if the calling user has an active connection.
2. Legacy SMTP via `email_settings`, if that table has a row.
3. Neither — returns ``(False, None, "no_email_provider_connected")``
   so the UI can be honest about non-delivery.

All three transactional callers were updated in the same commit; ad-hoc
mailbox sends (`/api/outlook/send`) are unaffected.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Graph /me/sendMail rejects the WHOLE message (not just the attachment) when
# inline fileAttachments push the request past ~4MB — and base64 inflates the
# raw bytes by ~33%. Callers attaching generated PDFs must skip the attachment
# above this raw-byte cap so an oversized render degrades to the html-only
# email instead of silently delivering nothing.
MAX_INLINE_ATTACHMENT_BYTES = 2_500_000


def _to_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _try_outlook_graph(
    *,
    tenant_db: Session,
    tenant_id: UUID,
    user_id: UUID,
    to_email: str,
    subject: str,
    html_body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    # One retry on the documented OutlookTransientRetry contract — a 401
    # mid-call after a successful refresh deserves exactly one re-issue.
    from gdx_dispatch.core.database import SessionLocal
    from gdx_dispatch.modules.outlook.token_refresh import (
        OutlookReconnectRequired,
        OutlookTransientRetry,
        with_outlook_client,
    )

    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "html", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if attachments:
        # Same wire shape as modules/outlook/send_router._graph_attachments.
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": a.get("name") or "attachment",
                "contentType": a.get("content_type") or "application/octet-stream",
                "contentBytes": a["content_base64"],
            }
            for a in attachments
        ]
    body = {
        "message": message,
        # Save to sent items so the rep can see it in their Outlook history
        # and the customer's reply lands in the same thread.
        "saveToSentItems": True,
    }

    def _send_once() -> None:
        # New control_db per attempt — the context manager's tenant_id
        # stash on .info is a per-session artifact, not safe to reuse.
        with SessionLocal() as control_db, with_outlook_client(control_db, tenant_db, user_id, tenant_id) as gc:
            gc._request("POST", "/me/sendMail", json=body)

    # Distinguish "never connected" (the user has no outlook_accounts row)
    # from "expired/needs reconnect" (had tokens but refresh failed). The
    # former should prompt the user to *connect*; the latter to *reconnect*.
    try:
        _send_once()
        return True, None
    except OutlookTransientRetry:
        try:
            _send_once()
            return True, None
        except Exception:
            log.exception("outlook_send_retry_failed user=%s", user_id)
            return False, "outlook_send_failed"
    except OutlookReconnectRequired as exc:
        msg = str(exc) if exc.args else ""
        if "has not connected" in msg or "no refresh token" in msg:
            reason = "outlook_not_connected"
        else:
            reason = "outlook_reconnect_required"
        log.info(
            "outlook_send_skipped: %s user=%s tenant=%s",
            reason, user_id, tenant_id,
        )
        return False, reason
    except Exception:
        log.exception(
            "outlook_send_failed user=%s tenant=%s — falling back to SMTP",
            user_id, tenant_id,
        )
        return False, "outlook_send_failed"


def _try_smtp(
    *,
    tenant_db: Session,
    tenant_id: str,
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    # Returns (sent, skip_reason). skip_reason is set when SMTP is just
    # not configured so the caller can distinguish "tried and failed"
    # from "wasn't an option".
    try:
        from gdx_dispatch.core.email_sender import get_email_config, send_email
        cfg = get_email_config(tenant_db, tenant_id)
        if cfg is None:
            return False, "smtp_not_configured"
        sent = bool(
            send_email(
                tenant_db, tenant_id, to_email, subject, html_body, to_name,
                attachments=attachments,
            )
        )
        if sent:
            return True, None
        return False, "smtp_send_failed"
    except Exception:
        log.exception("smtp_send_failed tenant=%s", tenant_id)
        return False, "smtp_exception"


def _record_outbound(
    *,
    tenant_db: Session | None,
    tenant_id: str,
    initiator_kind: str,
    initiator_ref: str | None,
    kind: str | None,
    entity_type: str | None,
    entity_id: str | None,
    to_email: str,
    to_name: str,
    recipient_source: str | None,
    recipient_contact_id: str | None,
    subject: str,
    html_body: str,
    attachments: list[dict[str, Any]] | None,
    provider: str | None,
    sent: bool,
    skip_reason: str | None,
) -> None:
    """Append-only audit row for this send attempt (locked requirement:
    everything auditable). Rides the caller's session (every send path
    commits right after a send); if that session is unusable — mid-rollback,
    or None — a fresh session on the same bind (or SessionLocal) records it
    anyway. A logging failure must never block or fail the send itself."""
    from gdx_dispatch.models.tenant_models import OutboundEmail

    meta = [
        {
            "name": a.get("name") or "attachment",
            "content_type": a.get("content_type") or "application/octet-stream",
            "size_bytes": int(len(a.get("content_base64") or "") * 3 / 4),
        }
        for a in (attachments or [])
    ]

    def _row() -> OutboundEmail:
        return OutboundEmail(
            company_id=str(tenant_id or ""),
            initiator_kind=initiator_kind or "user",
            initiator_ref=initiator_ref,
            kind=kind,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            to_email=to_email or "",
            to_name=to_name or None,
            recipient_source=recipient_source,
            recipient_contact_id=recipient_contact_id,
            subject=subject or "",
            body_html=html_body or "",
            attachments_meta=meta or None,
            provider=provider,
            status="sent" if sent else "failed",
            skip_reason=skip_reason,
        )

    try:
        if tenant_db is not None:
            tenant_db.add(_row())
            tenant_db.flush()
            return
    except Exception:
        log.exception("outbound_email_audit_primary_write_failed tenant=%s", tenant_id)
    try:
        from sqlalchemy.orm import Session as _Session

        if tenant_db is not None:
            bind = tenant_db.get_bind()
            with _Session(bind=bind) as audit_db:
                audit_db.add(_row())
                audit_db.commit()
        else:
            from gdx_dispatch.core.database import SessionLocal
            with SessionLocal() as audit_db:
                audit_db.add(_row())
                audit_db.commit()
    except Exception:
        log.exception("outbound_email_audit_write_failed tenant=%s", tenant_id)


def recently_sent(
    db: Session,
    entity_type: str,
    entity_id: str,
    *,
    within_seconds: int = 20,
    kind: str | None = None,
) -> bool:
    """Server-side double-send guard (Phase 5.5): True when this entity had a
    SUCCESSFUL send inside the window. A second tab, a retried request, or a
    re-clicked bulk row used to mean two identical customer emails — the
    only guards were frontend button flags. Uses the outbound_emails audit
    trail, so it needs no new state and can't disagree with it.

    Honest limit (adversarial audit round 2): this is check-then-act over
    COMMITTED rows — two requests in flight in the same instant both pass
    (the audit row commits at request end). It reliably stops the sequential
    re-click/retry class, which is the observed failure mode; a same-instant
    race needs a DB constraint this table can't express. Accepted."""
    try:
        from datetime import timedelta

        from gdx_dispatch.core.audit import utcnow
        from gdx_dispatch.models.tenant_models import OutboundEmail

        cutoff = utcnow() - timedelta(seconds=within_seconds)
        q = select(OutboundEmail.id).where(
            OutboundEmail.entity_type == entity_type,
            OutboundEmail.entity_id == str(entity_id),
            OutboundEmail.status == "sent",
            OutboundEmail.created_at >= cutoff,
        )
        if kind:
            q = q.where(OutboundEmail.kind == kind)
        return db.execute(q.limit(1)).first() is not None
    except Exception:
        # The guard must never block a legitimate send.
        log.exception("recently_sent_check_failed entity=%s", entity_id)
        return False


def send_transactional_email(
    *,
    tenant_db: Session,
    tenant_id: str,
    user_id: str | None,
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    attachments: list[dict[str, Any]] | None = None,
    initiator_kind: str = "user",
    initiator_ref: str | None = None,
    kind: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    recipient_source: str | None = None,
    recipient_contact_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Send a transactional email. Returns (sent, provider, skip_reason).

    - sent: True only when a provider acknowledged delivery.
    - provider: "outlook_graph" | "smtp" | None.
    - skip_reason: a short stable code naming why nothing went out, or
      None on success. Used by the UI to give the user an actionable
      message instead of a generic error.
    - attachments: [{name, content_type, content_base64}] — delivered by
      whichever provider wins (Graph fileAttachment / SMTP MIME part).
    - initiator_* / entity_* / recipient_*: audit-trail context recorded in
      outbound_emails for EVERY attempt. initiator_ref defaults to user_id
      for 'user' sends.

    Every call — success, failure, or missing recipient — writes one
    outbound_emails row; that is the delivery audit trail, not the logs.
    """
    def _finish(sent: bool, provider: str | None, skip: str | None):
        _record_outbound(
            tenant_db=tenant_db,
            tenant_id=str(tenant_id or ""),
            initiator_kind=initiator_kind,
            initiator_ref=initiator_ref or (str(user_id) if user_id and initiator_kind == "user" else initiator_ref),
            kind=kind,
            entity_type=entity_type,
            entity_id=entity_id,
            to_email=to_email,
            to_name=to_name,
            recipient_source=recipient_source,
            recipient_contact_id=recipient_contact_id,
            subject=subject,
            html_body=html_body,
            attachments=attachments,
            provider=provider,
            sent=sent,
            skip_reason=skip,
        )
        # email.sent / email.send_failed into the domain-event stream (P6):
        # webhook subscribers and consented plugins (the n8n path) can react
        # to delivery outcomes. emit_domain_event never raises into callers
        # and stages nothing when nobody listens.
        if tenant_db is not None and tenant_id:
            try:
                from gdx_dispatch.core.webhooks.emit import emit_domain_event

                emit_domain_event(
                    tenant_db,
                    "email.sent" if sent else "email.send_failed",
                    str(entity_id or to_email or ""),
                    {
                        "to_email": to_email,
                        "subject": subject,
                        "kind": kind,
                        "entity_type": entity_type,
                        "entity_id": str(entity_id) if entity_id else None,
                        "provider": provider,
                        "skip_reason": skip,
                        "initiator_kind": initiator_kind,
                        "initiator_ref": initiator_ref,
                        "company_id": str(tenant_id),
                    },
                    tenant_id=str(tenant_id),
                )
            except Exception:
                log.exception("email_event_emit_failed")
        return sent, provider, skip

    if not to_email:
        return _finish(False, None, "no_recipient_email")

    tid = _to_uuid(tenant_id)
    uid = _to_uuid(user_id)

    # 1. Outlook Graph as the calling user. Requires both ids; missing
    # either means we can't authenticate as a specific person.
    outlook_reason: str | None = None
    if tid is not None and uid is not None:
        sent_ol, outlook_reason = _try_outlook_graph(
            tenant_db=tenant_db,
            tenant_id=tid,
            user_id=uid,
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            attachments=attachments,
        )
        if sent_ol:
            return _finish(True, "outlook_graph", None)

    # 2. SMTP via email_settings.
    sent_smtp, smtp_reason = _try_smtp(
        tenant_db=tenant_db,
        tenant_id=str(tenant_id),
        to_email=to_email,
        to_name=to_name,
        subject=subject,
        html_body=html_body,
        attachments=attachments,
    )
    if sent_smtp:
        return _finish(True, "smtp", None)

    # 3. Neither path delivered. Pick the most informative reason: prefer
    # the Outlook diagnosis when Outlook was actually attempted; otherwise
    # surface the SMTP one. Pre-S110 state (no Outlook + no SMTP) returns
    # "no_email_provider_connected" which the UI maps to a connect-or-
    # configure call to action.
    if smtp_reason == "smtp_not_configured":
        if outlook_reason in ("outlook_not_connected", None):
            return _finish(False, None, "no_email_provider_connected")
        return _finish(False, None, outlook_reason)
    return _finish(False, None, smtp_reason or outlook_reason or "send_failed")
