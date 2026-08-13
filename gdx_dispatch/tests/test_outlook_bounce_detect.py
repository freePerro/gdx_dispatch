"""Bounce (NDR) detection — an email rejection must un-claim delivery.

Root incident (2026-08-13, EST-000085): an estimate email to a mistyped
address bounced 14 seconds after send; the estimate said "sent" forever
and nobody was told. These tests pin the three matching rungs against
the REAL prod NDR shape (Exchange system sender, "Undeliverable:"
subject, failed recipient in toRecipients + body preview).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice
from gdx_dispatch.modules.outlook.bounce_detect import process_bounces
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage
from gdx_dispatch.modules.proposals.models import Estimate

NOW = datetime.now(timezone.utc)

# Real prod NDR sender (Exchange's well-known NDR mailbox GUID).
NDR_FROM = "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@garagedoorxperts.com"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


def _mk_customer(db, email="cust@example.com", name="Cust"):
    c = Customer(name=name, email=email, company_id="tenant-test")
    db.add(c)
    db.commit()
    return c


def _mk_estimate(db, customer, *, number=None, status="sent", sent_at=None,
                 label=None):
    e = Estimate(
        estimate_number=number or f"EST-{uuid4().hex[:8]}",
        customer_id=customer.id if customer else None,
        status=status,
        sent_at=sent_at,
        label=label,
        company_id="tenant-test",
        public_token=uuid4().hex,
    )
    db.add(e)
    db.commit()
    return e


def _mk_invoice(db, customer, *, number=None, status="sent", sent_at=None):
    inv = Invoice(
        invoice_number=number or f"INV-{uuid4().hex[:8]}",
        customer_id=customer.id if customer else None,
        status=status,
        sent_at=sent_at,
        sent_via="email" if sent_at else None,
        company_id="tenant-test",
        public_token=uuid4().hex,
    )
    db.add(inv)
    db.commit()
    return inv


def _mk_msg(db, account, *, subject, from_address=NDR_FROM, to=None,
            preview="", conversation=None, received=None, sent=None,
            direction="inbound"):
    m = OutlookMessage(
        account_id=account.id,
        graph_message_id=uuid4().hex,
        subject=subject,
        from_address=from_address,
        to_addresses=to or [],
        body_preview=preview,
        conversation_id=conversation,
        received_at=received,
        sent_at=sent,
        direction=direction,
    )
    db.add(m)
    db.commit()
    return m


def _audit_actions(db):
    return [
        r.action
        for r in db.execute(select(AuditLog)).scalars().all()
    ]


# ── rung 1: serial number in the NDR subject ───────────────────────────


def test_estimate_flips_on_serial_in_subject(db, account):
    cust = _mk_customer(db)
    est = _mk_estimate(db, cust, number="EST-000042",
                       sent_at=NOW - timedelta(minutes=10))
    _mk_msg(db, account,
            subject="Undeliverable: Estimate #EST-000042 from GDX",
            to=["bad@x.com"], received=NOW)

    totals = process_bounces(db, account)

    assert totals["estimates_rejected"] == 1
    db.refresh(est)
    assert est.status == "rejected"
    assert est.sent_at is not None  # the attempt stays on record
    assert "estimate_email_rejected" in _audit_actions(db)


def test_invoice_clears_delivery_facts_on_serial_in_subject(db, account):
    cust = _mk_customer(db)
    inv = _mk_invoice(db, cust, number="INV-0007",
                      sent_at=NOW - timedelta(minutes=10))
    _mk_msg(db, account,
            subject="Undeliverable: Invoice #INV-0007 from GDX",
            to=["bad@x.com"], received=NOW)

    totals = process_bounces(db, account)

    assert totals["invoices_unsent"] == 1
    db.refresh(inv)
    assert inv.sent_at is None       # delivery fact disproven → cleared
    assert inv.sent_via is None
    assert inv.status == "sent"      # lifecycle status untouched
    assert "invoice_email_rejected" in _audit_actions(db)


# ── rung 2: failed recipient == customer email ─────────────────────────


def test_estimate_flips_on_customer_email_match(db, account):
    cust = _mk_customer(db, email="bjfarms1888@gmail.com")
    est = _mk_estimate(db, cust, sent_at=NOW - timedelta(seconds=14))
    # Job-title subject (the composer default) — no serial to parse.
    _mk_msg(db, account, subject="Undeliverable: 9x7 estimate",
            to=["bjfarms1888@gmail.com"], received=NOW)

    process_bounces(db, account)

    db.refresh(est)
    assert est.status == "rejected"


def test_failed_recipient_parsed_from_body_preview(db, account):
    """Exchange's preview first line names the failed address even when
    toRecipients is empty on the synced row."""
    cust = _mk_customer(db, email="bad@x.com")
    est = _mk_estimate(db, cust, sent_at=NOW - timedelta(minutes=1))
    _mk_msg(db, account, subject="Undeliverable: some job",
            to=[],
            preview="Your message to bad@x.com couldn't be delivered.\r\n"
                    "bad wasn't found at x.com.",
            received=NOW)

    process_bounces(db, account)

    db.refresh(est)
    assert est.status == "rejected"


# ── rung 3: conversation-sibling time correlation ──────────────────────


def test_estimate_flips_via_conversation_time_correlation(db, account):
    """The EST-000085 case verbatim: the office already FIXED the customer
    email, so rung 2 whiffs — but the NDR threads with the Sent-Items
    original whose sentDateTime matches the estimate's sent_at stamp."""
    cust = _mk_customer(db, email="djfarms1888@gmail.com")  # typo fixed
    send_time = NOW - timedelta(minutes=3)
    est = _mk_estimate(db, cust, label="9x7",
                       sent_at=send_time + timedelta(seconds=2))
    _mk_msg(db, account, subject="9x7 estimate", direction="outbound",
            from_address="doug@garagedoorxperts.com",
            to=["bjfarms1888@gmail.com"], conversation="c-1",
            sent=send_time)
    _mk_msg(db, account, subject="Undeliverable: 9x7 estimate",
            to=["bjfarms1888@gmail.com"], conversation="c-1",
            received=NOW)

    process_bounces(db, account)

    db.refresh(est)
    assert est.status == "rejected"


def test_time_correlation_refuses_ambiguity(db, account):
    """Two estimates stamped within the slack window → rung 3 must flip
    NEITHER (a wrong 'rejected' is worse than a missed one)."""
    cust_a = _mk_customer(db, email="a@x.com")
    cust_b = _mk_customer(db, email="b@x.com")
    send_time = NOW - timedelta(minutes=3)
    est_a = _mk_estimate(db, cust_a, sent_at=send_time)
    est_b = _mk_estimate(db, cust_b, sent_at=send_time + timedelta(seconds=30))
    _mk_msg(db, account, subject="an estimate", direction="outbound",
            from_address="doug@garagedoorxperts.com",
            to=["gone@dead.com"], conversation="c-2", sent=send_time)
    _mk_msg(db, account, subject="Undeliverable: an estimate",
            to=["gone@dead.com"], conversation="c-2", received=NOW)

    totals = process_bounces(db, account)

    assert totals["estimates_rejected"] == 0
    db.refresh(est_a); db.refresh(est_b)
    assert est_a.status == "sent"
    assert est_b.status == "sent"


def test_time_correlation_requires_a_subject_tie(db, account):
    """A unique time hit whose subject references NEITHER the estimate's
    number nor its label must not flip — that's some OTHER email bouncing
    near an estimate send, not the estimate's email."""
    cust = _mk_customer(db, email="right@x.com")
    send_time = NOW - timedelta(minutes=3)
    est = _mk_estimate(db, cust, label="9x7", sent_at=send_time)
    _mk_msg(db, account, subject="Fwd: parts order", direction="outbound",
            from_address="doug@garagedoorxperts.com",
            to=["vendor@dead.com"], conversation="c-3", sent=send_time)
    _mk_msg(db, account, subject="Undeliverable: Fwd: parts order",
            to=["vendor@dead.com"], conversation="c-3", received=NOW)

    process_bounces(db, account)

    db.refresh(est)
    assert est.status == "sent"


def test_delivery_receipts_and_delay_dsns_are_not_bounces(db, account):
    """The same system senders emit delivery/read receipts and delay DSNs
    (mail that arrives on retry) — sender prefix alone must never count."""
    cust = _mk_customer(db, email="fine@x.com")
    est = _mk_estimate(db, cust, number="EST-000050",
                       sent_at=NOW - timedelta(minutes=2))
    _mk_msg(db, account,
            subject="Delivered: Estimate #EST-000050 from GDX",
            to=["fine@x.com"],
            preview="Your message has been delivered to the recipient.",
            received=NOW)
    _mk_msg(db, account,
            subject="Delivery delayed: Estimate #EST-000050 from GDX",
            to=["fine@x.com"],
            preview="Delivery is delayed to these recipients. It's taking "
                    "longer than expected — no action is needed yet.",
            received=NOW)

    totals = process_bounces(db, account)

    assert totals["ndrs_seen"] == 0
    db.refresh(est)
    assert est.status == "sent"


# ── guards ─────────────────────────────────────────────────────────────


def test_resend_after_bounce_is_never_unflipped(db, account):
    """sent_at NEWER than the NDR = the office already re-sent after the
    bounce. The stale NDR must not touch it."""
    cust = _mk_customer(db, email="bad@x.com")
    est = _mk_estimate(db, cust, sent_at=NOW + timedelta(minutes=30))
    _mk_msg(db, account, subject="Undeliverable: re-send race",
            to=["bad@x.com"], received=NOW)

    process_bounces(db, account)

    db.refresh(est)
    assert est.status == "sent"


def test_accepted_estimate_is_never_flipped(db, account):
    cust = _mk_customer(db, email="bad@x.com")
    est = _mk_estimate(db, cust, status="accepted",
                       sent_at=NOW - timedelta(minutes=10))
    _mk_msg(db, account, subject="Undeliverable: whatever",
            to=["bad@x.com"], received=NOW)

    process_bounces(db, account)

    db.refresh(est)
    assert est.status == "accepted"


def test_old_ndrs_beyond_lookback_are_history(db, account):
    """A 90-day backfill must not flip long-settled documents."""
    cust = _mk_customer(db, email="bad@x.com")
    est = _mk_estimate(db, cust, sent_at=NOW - timedelta(days=60))
    _mk_msg(db, account, subject="Undeliverable: ancient",
            to=["bad@x.com"], received=NOW - timedelta(days=59))

    totals = process_bounces(db, account)

    assert totals["ndrs_seen"] == 0
    db.refresh(est)
    assert est.status == "sent"


def test_ordinary_mail_is_not_an_ndr(db, account):
    cust = _mk_customer(db, email="cust@x.com")
    est = _mk_estimate(db, cust, sent_at=NOW - timedelta(minutes=5))
    _mk_msg(db, account, subject="Re: your estimate",
            from_address="cust@x.com", to=["doug@garagedoorxperts.com"],
            received=NOW)

    totals = process_bounces(db, account)

    assert totals["ndrs_seen"] == 0
    db.refresh(est)
    assert est.status == "sent"


def test_process_bounces_is_idempotent(db, account):
    cust = _mk_customer(db, email="bad@x.com")
    _mk_estimate(db, cust, sent_at=NOW - timedelta(minutes=5))
    _mk_msg(db, account, subject="Undeliverable: twice",
            to=["bad@x.com"], received=NOW)

    first = process_bounces(db, account)
    second = process_bounces(db, account)

    assert first["estimates_rejected"] == 1
    assert second["estimates_rejected"] == 0  # already rejected → no-op
    # exactly one audit row despite two runs
    assert _audit_actions(db).count("estimate_email_rejected") == 1
