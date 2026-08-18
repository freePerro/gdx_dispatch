"""NDR (bounce) detection — make "Sent" mean *delivered*.

2026-08-13, EST-000085: the office emailed an estimate to a mistyped
address, Exchange bounced it 14 seconds later, and the estimate said
"sent" forever. Nothing in the app ever read the NDR sitting in the
synced inbox. Graph's ``/me/sendMail`` returns 202 for almost anything —
the real rejection only ever arrives asynchronously, as an NDR message,
so the sync is the ONLY place delivery failure can be observed.

``process_bounces`` runs at the end of every mailbox sync. It scans
recently-received NDRs and matches each one back to the send it reports
on, using three rungs ordered by precision:

1. **Serial in subject** — transactional sends title the email
   "Estimate #EST-000085 from …" / "Invoice #INV-000123 from …", and the
   NDR subject is "Undeliverable: <original subject>".
2. **Failed recipient == customer email** — the Exchange NDR's
   ``toRecipients`` is the original (failed) recipient; match it to the
   customer on a recently-sent document. Whiffs when the office already
   fixed the typo on the customer record (the exact EST-000085 case).
3. **Conversation-sibling time correlation** — the NDR shares a
   ``conversation_id`` with the synced Sent-Items original, whose
   ``sentDateTime`` lands within seconds of the document's ``sent_at``
   stamp (composer flow marks sent immediately after the Graph send).
   Applied only when it isolates exactly ONE candidate.

On a match:
- **Estimate** still ``sent`` → status becomes ``rejected`` (the enum
  value already exists and the whole UI already renders it in danger
  red; the reopen + re-send flows already accept it). ``sent_at`` is
  kept as the record of the attempt — reports.py excludes "rejected"
  from close-rate decisions, so a bounce is not a customer "no".
- **Invoice** still ``sent``/``overdue`` → ``sent_at``/``sent_via`` are
  cleared. Those are DELIVERY facts (Billing's own Unsent tab defines
  them that way), and clearing them resurfaces the invoice in Unsent
  where the office re-sends it.

Every flip logs an audit event carrying the failed recipient and the
NDR's identity. Everything is idempotent by construction: a flipped
estimate is no longer ``sent`` and a cleared invoice has no ``sent_at``,
so re-scanning the same NDR next cycle is a no-op — no marker column,
no migration.

Known limit: a bounce can only be matched to documents this app stamped;
NDRs for password resets, dunning, or hand-written mail simply find no
candidate and are skipped (counted in the result dict).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage

log = logging.getLogger(__name__)

# The GUID mailbox is Exchange's well-known NDR sender (constant across
# tenants); postmaster/mailer-daemon cover non-Exchange relays.
NDR_SENDER_PREFIXES = (
    "microsoftexchange329e71ec88ae4615bbc36ab6ce41109e@",
    "postmaster@",
    "mailer-daemon@",
)
NDR_SUBJECT_PREFIX = "undeliverable:"

# NDRs older than this are history, not news — keeps a 90-day backfill
# from flipping long-settled documents.
BOUNCE_LOOKBACK_DAYS = 7
# How far back a bounced send may be. An NDR normally lands within
# minutes; days-later bounces (greylisting retries) still match.
MATCH_WINDOW = timedelta(days=14)
# Rung 3: |document.sent_at - original message sentDateTime|. The status
# stamp happens in the same request as the Graph send, so seconds apart.
TIME_CORRELATION_SLACK = timedelta(minutes=5)

# '#' optional (2026-08-18): server sends now subject via the tenant
# templates ("Invoice {{number}} from {{company}}" — no '#'); NDRs quote
# either form.
_EST_SUBJECT_RE = re.compile(r"Estimate #?(.+?) from ", re.IGNORECASE)
_INV_SUBJECT_RE = re.compile(r"Invoice #?(.+?) from ", re.IGNORECASE)
# Exchange NDR body: "Your message to bad@x.com couldn't be delivered."
_FAILED_RECIPIENT_RE = re.compile(
    r"Your message to\s+(\S+@\S+?)\s+couldn't be delivered", re.IGNORECASE
)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for tz-aware columns; coerce."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_ndr(msg: OutlookMessage) -> bool:
    """True only for FAILURE reports. The same system senders also emit
    delivery/read receipts ("Delivered: …", "Read: …") and delay DSNs
    ("Delivery delayed — the mail arrives on retry"); sender prefix alone
    would flip documents whose email was in fact delivered, so a failure
    shape is required: the Undeliverable subject, or failure wording in
    the preview. Localized (non-English) NDRs are a known miss."""
    subj = (msg.subject or "").strip().lower()
    if subj.startswith(NDR_SUBJECT_PREFIX):
        return True
    frm = (msg.from_address or "").strip().lower()
    if not frm.startswith(NDR_SENDER_PREFIXES):
        return False
    preview = (msg.body_preview or "").lower()
    return (
        "couldn't be delivered" in preview
        or "could not be delivered" in preview
        or "delivery has failed" in preview
        or "wasn't delivered" in preview
    )


def _failed_recipients(msg: OutlookMessage) -> set[str]:
    """Exchange addresses the NDR's toRecipients as the failed recipient;
    the body's first line names it too. Union both, lowercase."""
    out: set[str] = set()
    for addr in msg.to_addresses or []:
        if isinstance(addr, str) and "@" in addr:
            out.add(addr.strip().lower())
    m = _FAILED_RECIPIENT_RE.search(msg.body_preview or "")
    if m:
        out.add(m.group(1).strip().rstrip(".").lower())
    return out


def _in_window(sent_at: datetime | None, ndr_received: datetime) -> bool:
    """The bounced send precedes its NDR. sent_at newer than the NDR means
    the office already re-sent after the bounce — never touch that."""
    sent_at = _aware(sent_at)
    if sent_at is None:
        return False
    return (
        ndr_received - MATCH_WINDOW
        <= sent_at
        <= ndr_received + TIME_CORRELATION_SLACK
    )


def _original_send_time(
    tdb: Session, ndr: OutlookMessage
) -> datetime | None:
    """Rung 3 anchor: the Sent-Items original in the NDR's conversation."""
    if not ndr.conversation_id:
        return None
    sibling = tdb.execute(
        select(OutlookMessage)
        .where(
            OutlookMessage.account_id == ndr.account_id,
            OutlookMessage.conversation_id == ndr.conversation_id,
            OutlookMessage.direction == "outbound",
        )
        .order_by(OutlookMessage.sent_at.desc())
    ).scalars().first()
    return _aware(sibling.sent_at) if sibling is not None else None


def _audit(tdb: Session, *, action: str, entity_type: str, entity_id: str,
           ndr: OutlookMessage, matched_by: str, recipients: set[str]) -> None:
    log_audit_event_sync(
        db=tdb,
        tenant_id=None,
        user_id="bounce-detector",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details={
            "failed_recipient": sorted(recipients)[0] if recipients else None,
            "ndr_subject": (ndr.subject or "")[:200],
            "ndr_graph_message_id": ndr.graph_message_id,
            "matched_by": matched_by,
        },
    )


def _match_estimates(
    tdb: Session, ndr: OutlookMessage, ndr_received: datetime
) -> int:
    """Flip matched still-'sent' estimates to 'rejected'. Returns count."""
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.proposals.models import Estimate

    recipients = _failed_recipients(ndr)
    flipped = 0

    def _flip(est: Estimate, matched_by: str) -> None:
        nonlocal flipped
        est.status = "rejected"
        est.updated_at = datetime.now(timezone.utc)
        _audit(
            tdb,
            action="estimate_email_rejected",
            entity_type="estimate",
            entity_id=str(est.id),
            ndr=ndr,
            matched_by=matched_by,
            recipients=recipients,
        )
        flipped += 1

    # Rung 1 — serial number in the NDR subject.
    m = _EST_SUBJECT_RE.search(ndr.subject or "")
    if m:
        est = tdb.execute(
            select(Estimate).where(
                Estimate.estimate_number == m.group(1).strip(),
                Estimate.status == "sent",
                Estimate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if est is not None and _in_window(est.sent_at, ndr_received):
            _flip(est, "subject_serial")
            return flipped

    # Rung 2 — failed recipient is still the customer's email.
    if recipients:
        rows = tdb.execute(
            select(Estimate)
            .join(Customer, Customer.id == Estimate.customer_id)
            .where(
                func.lower(Customer.email).in_(recipients),
                Estimate.status == "sent",
                Estimate.deleted_at.is_(None),
            )
        ).scalars().all()
        hits = [e for e in rows if _in_window(e.sent_at, ndr_received)]
        for est in hits:
            _flip(est, "customer_email")
        if hits:
            return flipped

    # Rung 3 — time correlation with the Sent-Items original. Guarded
    # three ways, because time alone would let ANY bounced email sent
    # near an estimate flip it: the candidate must be inside the normal
    # send window, must be the UNIQUE estimate stamped within slack of
    # the original's sentDateTime, and the bounced subject must actually
    # reference the estimate (its number or its label — the composer
    # subject is built from them). Ambiguity or no tie = flip nothing;
    # a wrong 'rejected' is worse than a missed one.
    orig_sent = _original_send_time(tdb, ndr)
    if orig_sent is not None:
        subj = (ndr.subject or "").lower()

        def _subject_ties(e: Estimate) -> bool:
            number = (e.estimate_number or "").strip().lower()
            label = (e.label or "").strip().lower()
            return bool(
                (number and number in subj) or (label and label in subj)
            )

        rows = tdb.execute(
            select(Estimate).where(
                Estimate.status == "sent",
                Estimate.deleted_at.is_(None),
                Estimate.sent_at.is_not(None),
            )
        ).scalars().all()
        hits = [
            e for e in rows
            if _in_window(e.sent_at, ndr_received)
            and abs(_aware(e.sent_at) - orig_sent) <= TIME_CORRELATION_SLACK
        ]
        if len(hits) == 1 and _subject_ties(hits[0]):
            _flip(hits[0], "conversation_time")
    return flipped


def _match_invoices(
    tdb: Session, ndr: OutlookMessage, ndr_received: datetime
) -> int:
    """Clear sent_at/sent_via on matched bounced invoices. Returns count."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice

    recipients = _failed_recipients(ndr)
    cleared = 0

    def _clear(inv: Invoice, matched_by: str) -> None:
        nonlocal cleared
        inv.sent_at = None
        inv.sent_via = None
        _audit(
            tdb,
            action="invoice_email_rejected",
            entity_type="invoice",
            entity_id=str(inv.id),
            ndr=ndr,
            matched_by=matched_by,
            recipients=recipients,
        )
        cleared += 1

    # Rung 1 — invoice number in the NDR subject.
    m = _INV_SUBJECT_RE.search(ndr.subject or "")
    if m:
        inv = tdb.execute(
            select(Invoice).where(
                Invoice.invoice_number == m.group(1).strip(),
                Invoice.status.in_(("sent", "overdue")),
                Invoice.sent_at.is_not(None),
            )
        ).scalar_one_or_none()
        if inv is not None and _in_window(inv.sent_at, ndr_received):
            _clear(inv, "subject_serial")
            return cleared

    # Rung 2 — failed recipient is still the customer's email.
    if recipients:
        rows = tdb.execute(
            select(Invoice)
            .join(Customer, Customer.id == Invoice.customer_id)
            .where(
                func.lower(Customer.email).in_(recipients),
                Invoice.status.in_(("sent", "overdue")),
                Invoice.sent_at.is_not(None),
            )
        ).scalars().all()
        for inv in rows:
            if not _in_window(inv.sent_at, ndr_received):
                continue
            # Email-overhaul Phase 5.2: this rung used to clear sent_at on
            # ANY bounce to the customer's address — including a bounced
            # REMINDER or RECEIPT about an invoice that was delivered fine
            # (reminder subjects never match rung 1, so their NDRs all fell
            # through to here). The outbound_emails audit trail now tells us
            # what was actually sent last: only a bounced 'document' send
            # disproves delivery of the invoice itself. No audit row =
            # pre-overhaul send = keep the old behavior.
            latest = _latest_outbound_row(tdb, recipients, "invoice", str(inv.id))
            if latest is not None:
                _stamp_bounced(tdb, latest, ndr_received)
                if (latest.kind or "document") != "document":
                    _audit(
                        tdb,
                        action="invoice_email_bounce_ignored_non_document",
                        entity_type="invoice",
                        entity_id=str(inv.id),
                        ndr=ndr,
                        matched_by="customer_email",
                        recipients=recipients,
                    )
                    continue
            _clear(inv, "customer_email")
    return cleared


def _latest_outbound_row(tdb: Session, recipients: set[str], entity_type: str, entity_id: str):
    """Most recent outbound_emails row to any of these addresses about this
    entity — the audit trail that says WHAT bounced. None on pre-overhaul
    sends (no row) or read failure (never block bounce processing)."""
    try:
        from gdx_dispatch.models.tenant_models import OutboundEmail

        return tdb.execute(
            select(OutboundEmail)
            .where(
                func.lower(OutboundEmail.to_email).in_(recipients),
                OutboundEmail.entity_type == entity_type,
                OutboundEmail.entity_id == entity_id,
                OutboundEmail.status == "sent",
            )
            .order_by(OutboundEmail.created_at.desc())
            .limit(1)
        ).scalars().first()
    except Exception:
        log.exception("bounce_outbound_lookup_failed entity=%s", entity_id)
        return None


def _stamp_bounced(tdb: Session, row, ndr_received: datetime) -> None:
    """Record the bounce on the audit row (the one UPDATE the append-only
    table allows). Idempotent — first stamp wins."""
    try:
        if row.bounced_at is None:
            row.bounced_at = ndr_received
            tdb.flush()
            try:
                from gdx_dispatch.core.webhooks.emit import emit_domain_event

                emit_domain_event(
                    tdb,
                    "email.bounced",
                    str(row.entity_id or row.to_email or ""),
                    {
                        "to_email": row.to_email,
                        "subject": row.subject,
                        "kind": row.kind,
                        "entity_type": row.entity_type,
                        "entity_id": row.entity_id,
                        "outbound_email_id": str(row.id),
                        "company_id": str(row.company_id or ""),
                    },
                    tenant_id=str(row.company_id or "") or None,
                )
            except Exception:
                log.exception("email_bounced_event_emit_failed")
    except Exception:
        log.exception("bounce_stamp_failed outbound_email=%s", getattr(row, "id", None))


def process_bounces(tdb: Session, account: OutlookAccount) -> dict[str, Any]:
    """Scan recent NDRs for this account and flip what they disprove.

    Called at the end of every mailbox sync. Safe to re-run: matches gate
    on the document still claiming delivery.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=BOUNCE_LOOKBACK_DAYS)

    # DB prefilter on the NDR signature (subject/sender prefixes) so this
    # never scans the whole inbox; received_at filtering happens in Python
    # (SQLite stores tz-aware columns naive, so a bound aware datetime
    # compares lexicographically wrong).
    sig = [func.lower(OutlookMessage.subject).like(f"{NDR_SUBJECT_PREFIX}%")]
    sig.extend(
        func.lower(OutlookMessage.from_address).like(f"{p}%")
        for p in NDR_SENDER_PREFIXES
    )
    candidates = tdb.execute(
        select(OutlookMessage).where(
            OutlookMessage.account_id == account.id,
            OutlookMessage.direction == "inbound",
            OutlookMessage.received_at.is_not(None),
            or_(*sig),
        )
    ).scalars().all()

    estimates_flipped = 0
    invoices_cleared = 0
    ndrs_seen = 0
    for msg in candidates:
        received = _aware(msg.received_at)
        if received is None or received < cutoff or not _is_ndr(msg):
            continue
        ndrs_seen += 1
        try:
            estimates_flipped += _match_estimates(tdb, msg, received)
            invoices_cleared += _match_invoices(tdb, msg, received)
        except Exception:
            # One malformed NDR must not stall the others (or the sync).
            log.exception("bounce_detect: NDR %s failed", msg.graph_message_id)
            tdb.rollback()
    if estimates_flipped or invoices_cleared:
        tdb.commit()
        log.info(
            "bounce_detect account=%s ndrs=%d estimates_rejected=%d invoices_unsent=%d",
            account.id, ndrs_seen, estimates_flipped, invoices_cleared,
        )
    return {
        "ndrs_seen": ndrs_seen,
        "estimates_rejected": estimates_flipped,
        "invoices_unsent": invoices_cleared,
    }
