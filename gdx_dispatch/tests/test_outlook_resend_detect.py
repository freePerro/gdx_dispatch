"""Manual re-send detection — rejected → sent when the operator re-sends
from the mailbox (estimate-rejection-visibility plan, PR 3).

The build-plan audit named three false flips a naive detector would make;
each one is a test here that must stay red-proof: a PDF forwarded to an
installer, the NDR forwarded to a coworker, and a message whose subject
carries a generic label. Every rung requires the message to be addressed
to the customer's current email.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.modules.estimates_features.service import EstimatesFeatures
from gdx_dispatch.modules.outlook.models import OutlookAccount
from gdx_dispatch.modules.outlook.resend_detect import process_resends
from gdx_dispatch.tests.test_outlook_bounce_detect import (
    NOW,
    _mk_customer,
    _mk_estimate,
    _mk_msg,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def account(db):
    acc = OutlookAccount(user_id="user-1", upn="doug@garagedoorxperts.com")
    db.add(acc)
    db.commit()
    return acc

@pytest.fixture(autouse=True)
def _features_default(monkeypatch):
    """_apply_send_expiry reads tenant features through the control DB,
    which this harness does not have; pin the 60-day default so the
    valid_until assertion is deterministic and the log stays clean."""
    import gdx_dispatch.routers.estimates as est_router
    monkeypatch.setattr(est_router, "get_features", lambda _tid: EstimatesFeatures())


OWNER = "doug@garagedoorxperts.com"
NDR_AT = NOW - timedelta(hours=2)          # the bounce arrived two hours ago
AUDIT_AT = NDR_AT + timedelta(minutes=30)  # …and the sync recorded it 30 min later


def _bounced(db, account, *, customer_email="bad@dead.example", number="EST-000085",
             label="16x7 Insulated Door", with_audit=True, with_ndr=True, conversation="c-85"):
    """A rejected estimate the way process_bounces leaves one: status
    rejected, an estimate_email_rejected audit row naming the NDR, and the
    NDR itself synced (received two hours ago)."""
    cust = _mk_customer(db, email=customer_email)
    est = _mk_estimate(db, cust, number=number, status="rejected", label=label,
                       sent_at=NDR_AT - timedelta(minutes=1))
    ndr = None
    if with_ndr:
        ndr = _mk_msg(db, account, subject=f"Undeliverable: {label}",
                      to=["typo@dead.example"], conversation=conversation, received=NDR_AT)
    if with_audit:
        # Written directly (audit_logs is append-only — no post-hoc UPDATE
        # of created_at): the row is stamped when the SYNC saw the bounce,
        # not when it arrived — 30 minutes after the NDR here.
        db.add(AuditLog(
            tenant_id=None, user_id="bounce-detector",
            action="estimate_email_rejected", entity_type="estimate", entity_id=str(est.id),
            details={"failed_recipient": "typo@dead.example",
                     "ndr_graph_message_id": ndr.graph_message_id if ndr else None,
                     "matched_by": "conversation_time"},
            created_at=AUDIT_AT,
        ))
        db.commit()
    return cust, est


def _outbound(db, account, *, subject, to, sent, conversation=None, cc=None,
              attachment=True, preview=""):
    """An outbound message. `attachment=True` by default: a re-send carries
    the PDF; the negative tests pass attachment=False on purpose."""
    m = _mk_msg(db, account, subject=subject, from_address=OWNER, to=to,
                conversation=conversation, sent=sent, direction="outbound",
                preview=preview)
    m.has_attachments = bool(attachment)
    if cc:
        m.cc_addresses = cc
    db.commit()
    return m


def _resend_audits(db):
    return db.execute(select(AuditLog).where(AuditLog.action == "estimate_resend_detected")).scalars().all()


# ── the three rungs ───────────────────────────────────────────────────────


def test_serial_in_subject_to_customer_flips(db, account):
    cust, est = _bounced(db, account)
    msg = _outbound(db, account, subject="Estimate EST-000085 from Garage Door Xperts",
                    to=["bad@dead.example"], sent=NDR_AT + timedelta(hours=1))

    totals = process_resends(db, account)

    assert totals == {"rejected_seen": 1, "resent_detected": 1}
    db.refresh(est)
    assert est.status == "sent"
    assert est.sent_via == "manual"
    assert est.sent_at.replace(tzinfo=None) == msg.sent_at.replace(tzinfo=None)
    assert est.valid_until is not None and est.valid_until > est.sent_at  # fresh window
    (audit,) = _resend_audits(db)
    assert audit.user_id == "resend-detector"
    assert audit.details["matched_by"] == "subject_serial"
    assert audit.details["recipient"] == "bad@dead.example"
    assert audit.details["graph_message_id"] == msg.graph_message_id


def test_same_conversation_to_customer_flips(db, account):
    """A reply/forward of the bounced thread, now to the corrected address."""
    cust, est = _bounced(db, account, customer_email="right@farm.example")
    # Subject deliberately carries neither the serial nor the label — only
    # the thread ties it.
    _outbound(db, account, subject="RE: your estimate (resending — sorry for the typo)",
              to=["right@farm.example"], conversation="c-85", sent=NDR_AT + timedelta(minutes=45))

    process_resends(db, account)

    db.refresh(est)
    assert est.status == "sent"
    assert _resend_audits(db)[0].details["matched_by"] == "conversation"


def test_label_in_subject_to_customer_flips(db, account):
    """The composer subject IS the label, so a plain re-compose carries it."""
    cust, est = _bounced(db, account, customer_email="right@farm.example")
    _outbound(db, account, subject="16x7 Insulated Door", to=["right@farm.example"],
              sent=NDR_AT + timedelta(hours=3))

    process_resends(db, account)

    db.refresh(est)
    assert est.status == "sent"
    assert _resend_audits(db)[0].details["matched_by"] == "subject_label"


def test_cc_counts_as_addressed(db, account):
    cust, est = _bounced(db, account, customer_email="right@farm.example")
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["spouse@farm.example"],
              cc=["right@farm.example"], sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "sent"


# ── the false flips the audit named — each must NOT flip ───────────────────


def test_pdf_forwarded_to_installer_does_not_flip(db, account):
    """Serial in the subject, wrong recipient: the office sent the PDF to an
    installer. Without the recipient guard this flipped to 'sent'."""
    cust, est = _bounced(db, account)
    _outbound(db, account, subject="FW: Estimate EST-000085 from Garage Door Xperts",
              to=["installer@crew.example"], sent=NDR_AT + timedelta(hours=1))

    totals = process_resends(db, account)

    assert totals["resent_detected"] == 0
    db.refresh(est)
    assert est.status == "rejected"
    assert _resend_audits(db) == []


def test_ndr_forwarded_to_coworker_does_not_flip(db, account):
    """Same conversation as the bounce, wrong recipient."""
    cust, est = _bounced(db, account)
    _outbound(db, account, subject="FW: Undeliverable: 16x7 Insulated Door",
              to=["office2@garagedoorxperts.com"], conversation="c-85",
              sent=NDR_AT + timedelta(minutes=10))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_generic_label_never_ties(db, account):
    """mobile_quoting stamps label = service or "Quote". Mail to the customer
    that merely says "Quote" is not evidence of THIS estimate."""
    cust, est = _bounced(db, account, label="Quote", customer_email="c@farm.example")
    _outbound(db, account, subject="Quote", to=["c@farm.example"], sent=NDR_AT + timedelta(hours=1))
    _outbound(db, account, subject="Your quote is ready", to=["c@farm.example"],
              sent=NDR_AT + timedelta(hours=2))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_unrelated_mail_to_customer_does_not_flip(db, account):
    cust, est = _bounced(db, account, customer_email="c@farm.example")
    _outbound(db, account, subject="Thanks for calling today", to=["c@farm.example"],
              sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_message_before_the_bounce_does_not_flip(db, account):
    """The original send itself (and anything else before the NDR) is not a re-send."""
    cust, est = _bounced(db, account)
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["bad@dead.example"],
              sent=NDR_AT - timedelta(minutes=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_anchor_is_the_ndr_arrival_not_the_sync_time(db, account):
    """The audit row is written when the sync SAW the bounce (up to a poll
    window later). An operator who re-sent in between must still be seen."""
    cust, est = _bounced(db, account)
    # After the NDR arrived, before the sync recorded it.
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["bad@dead.example"],
              sent=NDR_AT + timedelta(minutes=10))
    assert NDR_AT + timedelta(minutes=10) < AUDIT_AT
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "sent"


def test_only_rejected_estimates_are_touched(db, account):
    cust = _mk_customer(db, email="c@farm.example")
    for status in ("draft", "sent", "accepted", "declined", "expired"):
        _mk_estimate(db, cust, number=f"EST-{status}", status=status,
                     sent_at=NDR_AT - timedelta(minutes=1))
    _outbound(db, account, subject="Estimate EST-accepted from GDX", to=["c@farm.example"],
              sent=NDR_AT + timedelta(hours=1))
    totals = process_resends(db, account)
    assert totals == {"rejected_seen": 0, "resent_detected": 0}
    assert _resend_audits(db) == []


def test_customer_without_email_is_skipped(db, account):
    cust, est = _bounced(db, account, customer_email="")
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["someone@x.example"],
              sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_legacy_row_without_audit_falls_back_to_updated_at(db, account):
    cust, est = _bounced(db, account, with_audit=False, with_ndr=False)
    est.updated_at = NDR_AT
    db.commit()
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["bad@dead.example"],
              sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "sent"


def test_second_run_is_a_no_op(db, account):
    cust, est = _bounced(db, account)
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["bad@dead.example"],
              sent=NDR_AT + timedelta(hours=1))
    first = process_resends(db, account)
    second = process_resends(db, account)
    assert first["resent_detected"] == 1
    assert second == {"rejected_seen": 0, "resent_detected": 0}
    assert len(_resend_audits(db)) == 1


# ── the audit's scenarios (S2/S3/S4/S5) and the unpinned guards ──────────


def test_bare_reply_carrying_the_label_does_not_flip(db, account):
    """S3. On prod the estimate email subject IS the label, so "RE: <label>"
    to the customer satisfies every subject rung. Only the document — an
    attachment or the /proposals/ link — proves the customer now has it."""
    cust, est = _bounced(db, account, customer_email="c@farm.example")
    _outbound(db, account, subject="RE: 16x7 Insulated Door — did you get this?",
              to=["c@farm.example"], sent=NDR_AT + timedelta(hours=1), attachment=False)
    _outbound(db, account, subject="Estimate EST-000085 from GDX",
              to=["c@farm.example"], sent=NDR_AT + timedelta(hours=2), attachment=False)
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_public_link_in_the_body_counts_as_the_document(db, account):
    cust, est = _bounced(db, account, customer_email="c@farm.example")
    _outbound(db, account, subject="16x7 Insulated Door", to=["c@farm.example"],
              sent=NDR_AT + timedelta(hours=1), attachment=False,
              preview="Here it is again: https://gdx.example.com/proposals/abc123 — thanks")
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "sent"


def test_skewed_original_send_is_not_its_own_recovery(db, account):
    """S2. A client clock ahead of the server stamps the ORIGINAL send's
    sentDateTime after the NDR's received_at. It is to the customer, has the
    PDF and carries the label — every rung says yes. It is still the
    bounced message: within TIME_CORRELATION_SLACK of estimate.sent_at."""
    cust, est = _bounced(db, account, customer_email="bad@dead.example")
    # est.sent_at = NDR_AT - 1 min; the mailbox says +30 s: 90 s of skew.
    _outbound(db, account, subject="16x7 Insulated Door", to=["bad@dead.example"],
              sent=NDR_AT + timedelta(seconds=30))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"
    # A real re-send well after it still counts.
    _outbound(db, account, subject="16x7 Insulated Door", to=["bad@dead.example"],
              sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "sent"


def test_resend_behind_hundreds_of_newer_messages_is_still_seen(db, account):
    """S4. A fixed 500-row scan missed a re-send once 500 newer outbound
    messages existed; the walk now stops at the bounce anchor, not a count."""
    cust, est = _bounced(db, account, customer_email="c@farm.example")
    _outbound(db, account, subject="Estimate EST-000085 from GDX", to=["c@farm.example"],
              sent=NDR_AT + timedelta(minutes=5))
    for i in range(620):
        _outbound(db, account, subject=f"Unrelated {i}", to=[f"x{i}@else.example"],
                  sent=NDR_AT + timedelta(minutes=10 + i), attachment=False)
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "sent"


def test_resend_that_bounces_again_ends_the_cycle_rejected(db, account):
    """S5. Same sync cycle: the re-send AND its new NDR arrive together.
    tasks.py runs re-sends first, then bounces — traced here in that
    order: rejected → sent → rejected, both flips audited."""
    from gdx_dispatch.modules.outlook.bounce_detect import process_bounces

    cust, est = _bounced(db, account, customer_email="bad@dead.example", number="EST-000090")
    resend_at = NDR_AT + timedelta(hours=1)
    _outbound(db, account, subject="Estimate EST-000090 from GDX", to=["bad@dead.example"],
              sent=resend_at)
    _mk_msg(db, account, subject="Undeliverable: Estimate EST-000090 from GDX",
            to=["bad@dead.example"], received=resend_at + timedelta(seconds=20))

    assert process_resends(db, account)["resent_detected"] == 1
    db.refresh(est)
    assert est.status == "sent"
    assert process_bounces(db, account)["estimates_rejected"] == 1
    db.refresh(est)
    assert est.status == "rejected"
    actions = [a for a in (r.action for r in db.execute(select(AuditLog)).scalars().all())
               if a in ("estimate_resend_detected", "estimate_email_rejected")]
    assert actions.count("estimate_resend_detected") == 1
    assert actions.count("estimate_email_rejected") == 2  # the seeded one + the new one


def test_short_label_never_ties(db, account):
    """MIN_LABEL_LEN pinned on its own: "Doors" is not in the generic list."""
    cust, est = _bounced(db, account, label="Doors", customer_email="c@farm.example")
    _outbound(db, account, subject="Doors", to=["c@farm.example"], sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"


def test_generic_long_label_never_ties(db, account):
    """GENERIC_LABELS pinned on its own: "garage door" is long enough."""
    cust, est = _bounced(db, account, label="garage door", customer_email="c@farm.example")
    _outbound(db, account, subject="Garage door", to=["c@farm.example"], sent=NDR_AT + timedelta(hours=1))
    process_resends(db, account)
    db.refresh(est)
    assert est.status == "rejected"
