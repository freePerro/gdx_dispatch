"""Manual re-send detection — a fixed address heals "Failed Email".

estimate-rejection-visibility plan, PR 3. The bounce detector flips an
estimate to ``rejected`` (rendered "Failed Email") when its email bounces.
The two in-app ways out — ``/send`` and ``/mark-sent`` — already restore
``sent``. The hole was the third way, the one EST-000085's original send
actually used: the operator fixes the address on the customer record and
re-sends from their own mail client. The app never sees that send, so the
tag said "Failed Email" until someone happened to click Mark sent.

``process_resends`` runs at the end of every mailbox sync, after
``process_bounces``. For each ``rejected`` estimate it scans synced
**outbound** mail sent after the bounce and looks for the re-send.

Every candidate must pass two guards before any rung is consulted — both
found by adversarial audit, both load-bearing:

- **Addressed to the customer's CURRENT email** (to or cc). Without it a
  PDF forwarded to an installer ("FW: Estimate EST-000085 from …"), the
  NDR forwarded to a coworker (same conversation), or any mail whose
  subject happens to carry the estimate's label would have flipped the
  estimate to ``sent`` — a wrong "sent" hides a bounce all over again.
- **Carries the document**: an attachment, or the public ``/proposals/``
  link in the body. On prod the estimate email's subject IS the label
  (``{{job_title}}`` = ``job.title or estimate.label``, and convert-to-job
  copies the label into the title), so a bare "RE: 16x7 Insulated Door"
  reply to the customer satisfies every subject rung; only the document
  proves the customer now HAS the estimate.
- The original send itself is never a re-send: anything stamped within
  ``TIME_CORRELATION_SLACK`` of the estimate's own ``sent_at`` is skipped,
  so a client clock ahead of the server cannot present the bounced message
  as its own recovery.

Then the message must tie to THIS estimate, by one of:

1. **Serial in subject** — "Estimate EST-000085 from …" / the number
   anywhere in the subject.
2. **Same conversation as the bounce** — a reply/forward of the original
   thread to the corrected address.
3. **Label in subject** — the composer subject IS the label, so a plain
   re-compose carries it. Guarded: labels shorter than six characters or
   generic ("Quote", "Estimate", …) never tie — mobile stamps
   ``label = service or "Quote"``.

The time anchor is the NDR's ``received_at`` (via the audit row's
``ndr_graph_message_id``), NOT the audit row's ``created_at``: the audit
row is written when the sync SAW the bounce, which can be a 30-minute poll
later, and an operator who re-sent inside that window would otherwise be
invisible forever. When the NDR is no longer synced the audit time is the
fallback; with no audit row at all (pre-#317 data) the estimate's
``updated_at`` is.

On a match: ``status = sent``, ``sent_at`` = the message's sentDateTime,
``sent_via = "manual"``, the tenant's send-expiry window re-applied, and
audit ``estimate_resend_detected`` by ``resend-detector`` (recipient, Graph
message id, matched_by). Only ``rejected → sent``, never any other
transition. If the re-send bounces too, ``process_bounces`` flips it right
back next cycle; every flip is audited, so the cycle is safe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync
from gdx_dispatch.modules.outlook.bounce_detect import (
    _EST_SUBJECT_RE,
    NDR_SENDER_PREFIXES,
    TIME_CORRELATION_SLACK,
    _aware,
)
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage

log = logging.getLogger(__name__)

# Rung 3 guards. A label this short, or one of these words, is not evidence
# that a message is about THIS estimate.
MIN_LABEL_LEN = 6
GENERIC_LABELS = frozenset({
    "quote", "estimate", "proposal", "bid", "door", "garage door",
    "repair", "service", "install", "installation", "new door",
})
# Outbound mail is read newest-first in pages and stops at the oldest
# bounce anchor among the rejected estimates — never a fixed row cap, which
# a busy Sent Items folder (500 newer messages) would have pushed a real
# re-send behind (audit catch, S4).
OUTBOUND_PAGE = 500
# Evidence that the message carries the estimate: the composer's public
# approval link, when it survives into the synced preview.
PROPOSAL_LINK_MARKER = "/proposals/"


def _addresses(msg: OutlookMessage) -> set[str]:
    out: set[str] = set()
    for field in (msg.to_addresses, msg.cc_addresses):
        for addr in field or []:
            if isinstance(addr, str) and "@" in addr:
                out.add(addr.strip().lower())
    return out


def _is_system_sender(msg: OutlookMessage) -> bool:
    frm = (msg.from_address or "").strip().lower()
    return frm.startswith(NDR_SENDER_PREFIXES)


def _carries_document(msg: OutlookMessage) -> bool:
    """An attachment (the PDF), or the public approval link in the body."""
    if bool(getattr(msg, "has_attachments", False)):
        return True
    return PROPOSAL_LINK_MARKER in (msg.body_preview or "").lower()


def _outbound_since(tdb: Session, account: OutlookAccount, floor: datetime) -> list[OutlookMessage]:
    """Outbound messages sent after ``floor``, newest-first in pages, no
    row cap: the walk stops at the first page whose oldest row is older
    than the floor. Time filtering is done in Python on purpose — SQLite
    stores tz-aware columns naive, so a bound aware datetime compares
    wrong in SQL."""
    out: list[OutlookMessage] = []
    offset = 0
    while True:
        page = tdb.execute(
            select(OutlookMessage)
            .where(
                OutlookMessage.account_id == account.id,
                OutlookMessage.direction == "outbound",
                OutlookMessage.sent_at.is_not(None),
            )
            .order_by(OutlookMessage.sent_at.desc(), OutlookMessage.id.desc())
            .offset(offset)
            .limit(OUTBOUND_PAGE)
        ).scalars().all()
        if not page:
            break
        for m in page:
            sent = _aware(m.sent_at)
            if sent is not None and sent > floor and not _is_system_sender(m):
                out.append(m)
        oldest = _aware(page[-1].sent_at)
        if len(page) < OUTBOUND_PAGE or oldest is None or oldest <= floor:
            break
        offset += OUTBOUND_PAGE
    return out


def _bounce_anchor(tdb: Session, est) -> tuple[datetime | None, str | None]:
    """(time the bounce arrived, the NDR's conversation_id).

    Prefers the synced NDR's received_at; falls back to the audit row's
    created_at, then to the estimate's updated_at (legacy rows).
    """
    row = tdb.execute(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "estimate",
            AuditLog.entity_id == str(est.id),
            AuditLog.action == "estimate_email_rejected",
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return _aware(getattr(est, "updated_at", None)), None
    anchor = _aware(row.created_at)
    conversation = None
    details = row.details if isinstance(row.details, dict) else {}
    gid = details.get("ndr_graph_message_id")
    if gid:
        ndr = tdb.execute(
            select(OutlookMessage).where(OutlookMessage.graph_message_id == gid).limit(1)
        ).scalars().first()
        if ndr is not None:
            conversation = ndr.conversation_id
            if ndr.received_at is not None:
                anchor = _aware(ndr.received_at)
    return anchor, conversation


def _subject_tie(subject: str, est) -> str | None:
    subj = (subject or "").strip()
    low = subj.lower()
    number = (est.estimate_number or "").strip().lower()
    if number:
        m = _EST_SUBJECT_RE.search(subj)
        if m and m.group(1).strip().lower() == number:
            return "subject_serial"
        if number in low:
            return "subject_serial"
    label = (est.label or "").strip().lower()
    if label and len(label) >= MIN_LABEL_LEN and label not in GENERIC_LABELS and label in low:
        return "subject_label"
    return None


def _match(msg: OutlookMessage, est, customer_email: str, conversation: str | None) -> str | None:
    """The rung this message satisfies for this estimate, or None. The
    recipient and document checks come first and are not optional."""
    if customer_email not in _addresses(msg):
        return None
    if not _carries_document(msg):
        return None
    tie = _subject_tie(msg.subject or "", est)
    if tie:
        return tie
    if conversation and msg.conversation_id and msg.conversation_id == conversation:
        return "conversation"
    return None


def _audit_resend(tdb: Session, est, msg: OutlookMessage, matched_by: str, recipient: str) -> None:
    log_audit_event_sync(
        db=tdb,
        tenant_id=None,
        user_id="resend-detector",
        action="estimate_resend_detected",
        entity_type="estimate",
        entity_id=str(est.id),
        details={
            "recipient": recipient,
            "graph_message_id": msg.graph_message_id,
            "matched_by": matched_by,
            "sent_at": _aware(msg.sent_at).isoformat() if msg.sent_at else None,
            "subject": (msg.subject or "")[:200],
        },
    )


def _reapply_send_expiry(est) -> None:
    """Same rule as /send and /mark-sent — a re-send gets a fresh window.
    Best-effort: the helper reads tenant features and already degrades to
    the 60-day default on its own."""
    try:
        from gdx_dispatch.routers.estimates import _apply_send_expiry

        _apply_send_expiry(est)
    except Exception:
        log.exception("resend_detect: send-expiry refresh failed estimate=%s", est.id)


def process_resends(tdb: Session, account: OutlookAccount) -> dict[str, Any]:
    """Flip rejected estimates that were re-sent from the mailbox. Safe to
    re-run: a flipped estimate is no longer rejected."""
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.proposals.models import Estimate

    rejected = tdb.execute(
        select(Estimate).where(
            Estimate.status == "rejected",
            Estimate.deleted_at.is_(None),
        )
    ).scalars().all()
    if not rejected:
        return {"rejected_seen": 0, "resent_detected": 0}

    # Resolve every candidate's anchor first: the outbound read stops at
    # the oldest one, so it is one walk shared by all of them.
    work: list[tuple[Any, str, datetime, str | None]] = []
    for est in rejected:
        try:
            customer_email = None
            if est.customer_id is not None:
                customer_email = tdb.execute(
                    select(Customer.email).where(Customer.id == est.customer_id)
                ).scalar_one_or_none()
            customer_email = (customer_email or "").strip().lower()
            if not customer_email:
                continue  # nothing to address-match against; never guess
            anchor, conversation = _bounce_anchor(tdb, est)
            if anchor is None:
                continue
            work.append((est, customer_email, anchor, conversation))
        except Exception:
            log.exception("resend_detect: anchor for estimate %s failed", getattr(est, "id", None))
    if not work:
        return {"rejected_seen": len(rejected), "resent_detected": 0}

    outbound = _outbound_since(tdb, account, min(w[2] for w in work))
    outbound.sort(key=lambda m: _aware(m.sent_at))  # oldest first: the FIRST re-send wins

    detected = 0
    for est, customer_email, anchor, conversation in work:
        try:
            original_sent = _aware(est.sent_at)
            for msg in outbound:
                sent = _aware(msg.sent_at)
                if sent is None or sent <= anchor:
                    continue
                if original_sent is not None and abs(sent - original_sent) <= TIME_CORRELATION_SLACK:
                    # The bounced send itself, seen through a skewed clock —
                    # never its own recovery (audit catch, S2).
                    continue
                matched_by = _match(msg, est, customer_email, conversation)
                if not matched_by:
                    continue
                est.status = "sent"
                est.sent_at = sent
                est.sent_via = "manual"
                est.updated_at = datetime.now(timezone.utc)
                _reapply_send_expiry(est)
                _audit_resend(tdb, est, msg, matched_by, customer_email)
                detected += 1
                log.info(
                    "resend_detect: %s rejected→sent via %s (message %s)",
                    est.estimate_number, matched_by, msg.graph_message_id,
                )
                break
        except Exception:
            log.exception("resend_detect: estimate %s failed", getattr(est, "id", None))
            tdb.rollback()
    if detected:
        tdb.commit()
    return {"rejected_seen": len(rejected), "resent_detected": detected}
