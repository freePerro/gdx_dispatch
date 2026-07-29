"""Record when a CUSTOMER opens a document we sent them.

Two public, unauthenticated endpoints are exactly what a customer hits after
clicking the link in an estimate or invoice email:

    GET /pay/{invoice_token}   -> core/payments.py
    GET /proposals/{token}     -> modules/proposals/router.py

Neither logged anything, so "did they even look at it?" was unanswerable.

This is deliberately click-through logging rather than an email open-tracking
pixel. A click means a human chose to look. An image fetch does not: Apple Mail
Privacy Protection pre-fetches every image on delivery (default-on since iOS
15), and Gmail proxies and caches them, so pixel "opens" are inflated on one
side and undercounted on the other. It also needs no new table, no send-path
change, and puts no tracker in anyone's mailbox.

Three things this has to get right, because the endpoints are public:

* **De-dupe.** Refreshing the pay page eight times is one viewing, not eight
  audit rows.
* **Bots.** Corporate mail gateways and Safe Links scanners fetch every URL in
  an email. Those are not the customer.
* **Write amplification.** An unauthenticated GET that writes a row is a DoS
  lever. The de-dupe check bounds it; the endpoints should still be rate
  limited independently.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync
from gdx_dispatch.core.audit_labels import PUBLIC_CUSTOMER_ACTOR

log = logging.getLogger(__name__)

#: A customer reading a document, refreshing, and paying is one visit. Long
#: enough to swallow a session; short enough that coming back tomorrow to look
#: again is recorded as a second, genuine view.
VIEW_DEDUPE_WINDOW = timedelta(minutes=30)

#: The actor for a public document view. The token is the only credential and
#: it identifies a document, not a person — several people at a company may
#: share the same link, so claiming to know WHICH customer contact looked would
#: be a lie. The subject (the invoice/estimate, hence its customer) is what
#: carries the identity here.
#: Imported from audit_labels so the writer and the resolver cannot drift.
#: They already did once: this was a bare "customer" string that
#: resolve_actors classified as an API key, so the customer badge — added one
#: phase earlier for exactly this — never fired.
CUSTOMER_ACTOR = PUBLIC_CUSTOMER_ACTOR

#: A view this soon after the document was sent is a link scanner, not a
#: person. Microsoft Defender Safe Links and Proofpoint URL Defense detonate
#: links with a REAL Chrome user-agent on purpose — defeating cloaking is the
#: whole design — so no UA string can catch them. Timing can: a human does not
#: open an estimate two seconds after it leaves the outbox.
#:
#: This is the primary discriminator. The UA list below is a cheap
#: second line for the scanners that do identify themselves.
SCANNER_GRACE_PERIOD = timedelta(seconds=90)

#: Substrings that mark a self-identifying automated fetch.
#:
#: `bot` is never matched bare. "CUBOT" is a real Android phone brand, and
#: "Mozilla/5.0 (Linux; Android 13; CUBOT NOTE 30)" contains both "bot" and
#: "bot " — a loose match silently dropped those customers entirely. Real bots
#: delimit the token: "Googlebot/2.1", "Pingdom.com_bot_version".
_BOT_UA_MARKERS = (
    "bot/",
    "bot;",
    "bot)",
    "_bot",
    "-bot",
    "crawler",
    "spider",
    "slurp",
    "preview",
    "scanner",
    "safelinks",
    "proofpoint",
    "mimecast",
    "barracuda",
    "curl/",
    "wget",
    "python-requests",
    "httpx",
    "headlesschrome",
    "monitoring",
    "pingdom",
    "uptime",
)


def looks_like_a_bot(user_agent: str | None) -> bool:
    if not user_agent:
        # No UA at all is not a browser. Real mail clients and browsers always
        # send one.
        return True
    ua = user_agent.lower().strip()
    if ua.endswith("bot"):
        return True
    return any(marker in ua for marker in _BOT_UA_MARKERS)


def within_scanner_grace_period(sent_at: Any) -> bool:
    """True if the document was sent so recently that this fetch is almost
    certainly the mail gateway following the link, not the recipient."""
    if not sent_at:
        return False
    try:
        stamp = sent_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return datetime.now(UTC) - stamp < SCANNER_GRACE_PERIOD
    except Exception:
        log.exception("scanner_grace_period_check_failed")
        return False


def _recently_recorded(db: Any, action: str, entity_id: str) -> bool:
    try:
        cutoff = datetime.now(UTC) - VIEW_DEDUPE_WINDOW
        existing = db.execute(
            select(AuditLog.id)
            .where(AuditLog.action == action)
            .where(AuditLog.entity_id == str(entity_id))
            .where(AuditLog.created_at >= cutoff)
            .limit(1)
        ).first()
        return existing is not None
    except Exception:
        # If the de-dupe probe fails we would rather write a duplicate row than
        # lose the event — but say so, because silently writing one row per
        # refresh on a public endpoint is the failure mode that matters.
        log.exception("customer_view_dedupe_check_failed action=%s", action)
        return False


def record_customer_view(
    db: Any,
    *,
    action: str,
    entity_type: str,
    entity_id: Any,
    tenant_id: str | None = None,
    request: Any = None,
    details: dict[str, Any] | None = None,
    sent_at: Any = None,
) -> bool:
    """Audit a customer opening a public document. Returns True if a row was
    written.

    Never raises: a failure here must not stop a customer from seeing their
    invoice or paying it.
    """
    try:
        user_agent = None
        headers = getattr(request, "headers", None)
        if headers is not None:
            getter = getattr(headers, "get", None)
            if callable(getter):
                user_agent = getter("user-agent")

        if looks_like_a_bot(user_agent):
            return False
        if within_scanner_grace_period(sent_at):
            # The gateway scans within seconds of send. Recording that as
            # "the customer opened it" is the single most misleading thing
            # this feature could do — the office would stop chasing a quote
            # nobody read.
            return False
        if _recently_recorded(db, action, str(entity_id)):
            return False

        payload = dict(details or {})
        # Keep the UA so a false "view" is diagnosable rather than mysterious.
        payload.setdefault("user_agent", (user_agent or "")[:200])
        payload.setdefault("via", "public_link")

        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=CUSTOMER_ACTOR,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=payload,
            request=request,
        )
        db.commit()
        return True
    except Exception:
        # Never raises: a customer must still be able to see and pay their
        # invoice when the audit write fails.
        log.exception("record_customer_view_failed action=%s", action)
        try:
            db.rollback()
        except Exception:
            log.exception("record_customer_view_rollback_failed")
        return False
