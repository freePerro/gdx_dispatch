"""The delayed-payment lifecycle reaches the office and the payer (GDXA-393).

Before: a bank debit that bounced, a charge that failed, a full refund made in
the Stripe Dashboard, a dispute — each was recorded in the audit log and rang
nothing; the customer got no receipt on any path; and the office could not see
a bank transfer that was still moving.

Every Stripe event here goes through the real ``POST /stripe/webhook`` route
with a real signature, into the real handler, on an ORM-built schema. The
assertions read back the rows that landed (``notifications``,
``outbound_emails``, ``audit_logs``), never mock call arguments: a mock proves
which arguments were passed, not which row was written.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Notification,
    OutboundEmail,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status

COMPANY = "11111111-1111-1111-1111-111111111111"
SECRET = "whsec_test_lifecycle_" + "x" * 24
INTENT = "pi_lifecycle_1"
CHARGE = "ch_lifecycle_1"
CENTS = 50_000


@pytest.fixture
def receipts(monkeypatch):
    """Invoice ids the payment path queued a receipt for. The queue itself
    has no broker in the test image; the send it would run is driven for
    real in the receipt tests below."""
    queued: list[str] = []
    monkeypatch.setattr(
        "gdx_dispatch.tasks.billing_followup.enqueue_payment_receipt",
        lambda invoice_id, **k: queued.append(str(invoice_id)) or True,
    )
    return queued


@pytest.fixture
def db(tenant_db, monkeypatch, receipts):
    monkeypatch.delenv("GDX_ENV", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(
        "gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep",
        lambda *a, **k: False,
    )
    # The failure events ask Stripe whether the attempt is current: yes.
    pi = MagicMock()
    pi.id, pi.status, pi.latest_charge = INTENT, "requires_payment_method", CHARGE
    monkeypatch.setattr("stripe.PaymentIntent.retrieve", lambda *a, **k: pi)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


@pytest.fixture
def client(db):
    from gdx_dispatch.routers.stripe_webhook import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture
def customer(db):
    cust = Customer(name="Pat Payer", email="pat@example.com", phone="555-0100", company_id=COMPANY)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


@pytest.fixture
def invoice(db, customer):
    inv = Invoice(
        id=uuid4(),
        customer_id=customer.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft",
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("500.00"),
        balance_due=Decimal("500.00"),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        invoice_id=inv.id, description="Opener install", quantity=1,
        unit_price=Decimal("500.00"), line_total=Decimal("500.00"), company_id=COMPANY,
    ))
    db.commit()
    transition_invoice_status(db, inv, "sent")
    db.commit()
    db.refresh(inv)
    return inv


def _deliver(client, etype: str, obj: dict) -> dict:
    event = {"id": f"evt_{uuid4().hex[:24]}", "object": "event", "type": etype, "data": {"object": obj}}
    body = json.dumps(event, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    resp = client.post(
        "/stripe/webhook", content=body,
        headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]  # the handler's own verdict


def _succeeded(client, invoice, rail="card"):
    return _deliver(client, "payment_intent.succeeded", {
        "id": INTENT, "object": "payment_intent", "status": "succeeded",
        "amount": CENTS, "amount_received": CENTS, "currency": "usd",
        "payment_method_types": [rail], "latest_charge": CHARGE,
        "metadata": {"invoice_id": str(invoice.id)},
    })


def _intent_failed(client, invoice, rail="us_bank_account", message="Insufficient funds"):
    return _deliver(client, "payment_intent.payment_failed", {
        "id": INTENT, "object": "payment_intent", "status": "requires_payment_method",
        "amount": CENTS, "latest_charge": CHARGE, "payment_method_types": [rail],
        "metadata": {"invoice_id": str(invoice.id)},
        "last_payment_error": {"code": "insufficient_funds", "message": message},
    })


def _charge_failed(client):
    return _deliver(client, "charge.failed", {"id": CHARGE, "object": "charge", "payment_intent": INTENT})


def _bells(db) -> list[Notification]:
    db.expire_all()
    return list(db.scalars(
        select(Notification).where(Notification.tenant_id == COMPANY).order_by(Notification.created_at)
    ).all())


def _titles(db) -> list[str]:
    return [n.title for n in _bells(db)]


# ── office alerts ───────────────────────────────────────────────────────────


def test_bounced_bank_debit_rings_the_office(client, db, invoice):
    """The usual ACH failure: the debit bounced while still processing, so no
    Payment row ever existed and the reversal found nothing. This is the one
    that rang nowhere at all."""
    out = _intent_failed(client, invoice)
    assert out["reversal"] == "no_payment_to_reverse"

    bells = _bells(db)
    assert [b.title for b in bells] == ["Bank payment failed"]
    msg = bells[0].message
    assert invoice.invoice_number in msg
    assert "$500.00" in msg
    assert "Insufficient funds" in msg
    assert "Pat Payer" in msg
    assert bells[0].category == "payment"
    assert bells[0].user_id is None  # broadcast to the office


def test_card_decline_does_not_ring(client, db, invoice):
    """A card decline happens on the customer's screen; ringing for every one
    would bury the alerts that matter."""
    _intent_failed(client, invoice, rail="card", message="Your card was declined.")
    _charge_failed(client)
    assert _titles(db) == []


@pytest.mark.parametrize(
    ("etype", "obj", "phrase"),
    [
        ("charge.refunded",
         {"id": CHARGE, "object": "charge", "payment_intent": INTENT,
          "amount": CENTS, "amount_refunded": CENTS, "refunded": True},
         "refunded in full"),
        ("charge.dispute.created",
         {"id": "dp_1", "object": "dispute", "charge": CHARGE, "payment_intent": INTENT,
          "amount": CENTS, "status": "needs_response"},
         "disputed"),
        ("charge.dispute.funds_withdrawn",
         {"id": "dp_1", "object": "dispute", "charge": CHARGE, "payment_intent": INTENT,
          "amount": CENTS, "status": "needs_response"},
         "withdrew the funds"),
        ("charge.failed",
         {"id": CHARGE, "object": "charge", "payment_intent": INTENT},
         "the charge failed"),
    ],
)
def test_every_webhook_reversal_rings_once(client, db, invoice, etype, obj, phrase):
    _succeeded(client, invoice)
    assert _titles(db) == ["Payment received"]

    assert _deliver(client, etype, obj)["status"] == "reversed"

    bells = _bells(db)
    assert [b.title for b in bells] == ["Payment received", "Payment reversed"]
    msg = bells[1].message
    assert invoice.invoice_number in msg and "$500.00" in msg and phrase in msg
    assert "open again" in msg
    # And the money really did come off: the bell describes a fact.
    db.expire_all()
    assert db.get(Invoice, invoice.id).status == "sent"


def test_bank_return_after_settlement_rings_once_not_twice(client, db, invoice):
    """Stripe sends `charge.failed` AND `payment_intent.payment_failed` for one
    failed attempt. The first reverses and rings; the second finds nothing
    left to reverse and must stay quiet rather than ring "failed" again."""
    _succeeded(client, invoice, rail="us_bank_account")
    _charge_failed(client)
    _intent_failed(client, invoice)

    assert _titles(db) == ["Payment received", "Payment reversed"]


def test_dispute_won_rings(client, db, invoice):
    dispute = {"id": "dp_1", "object": "dispute", "charge": CHARGE, "payment_intent": INTENT,
               "amount": CENTS, "status": "needs_response"}
    _succeeded(client, invoice)
    _deliver(client, "charge.dispute.funds_withdrawn", dispute)
    assert _deliver(client, "charge.dispute.funds_reinstated", {**dispute, "status": "won"})["status"] == "reinstated"

    assert _titles(db) == ["Payment received", "Payment reversed", "Dispute closed in your favor"]


def test_dispute_inquiry_does_not_ring(client, db, invoice):
    """A `warning_*` inquiry moves no money and reverses nothing."""
    _succeeded(client, invoice)
    _deliver(client, "charge.dispute.created", {
        "id": "dp_1", "object": "dispute", "charge": CHARGE, "payment_intent": INTENT,
        "amount": CENTS, "status": "warning_needs_response",
    })
    assert _titles(db) == ["Payment received"]


def test_partial_refund_tells_the_office_to_record_it(client, db, invoice):
    _succeeded(client, invoice)
    out = _deliver(client, "charge.refunded", {
        "id": CHARGE, "object": "charge", "payment_intent": INTENT,
        "amount": CENTS, "amount_refunded": 5_000, "refunded": False,
    })
    assert out["status"] == "partial_refund_not_recorded"

    bells = _bells(db)
    assert [b.title for b in bells] == ["Payment received", "Partial refund at Stripe — check it is recorded"]
    assert "$50.00" in bells[1].message and "Payments page" in bells[1].message


def test_a_second_partial_refund_says_the_amount_is_a_running_total(client, db, invoice):
    """Stripe's amount_refunded is cumulative: $50 then $30 arrives as 5,000
    then 8,000 cents. The second bell must not read as an $80 refund to
    record, or an office that recorded the $50 overstates refunds by $50."""
    _succeeded(client, invoice)
    for refunded in (5_000, 8_000):
        _deliver(client, "charge.refunded", {
            "id": CHARGE, "object": "charge", "payment_intent": INTENT,
            "amount": CENTS, "amount_refunded": refunded, "refunded": False,
        })

    second = _bells(db)[-1].message
    assert "$80.00" in second and "IN TOTAL so far" in second, second
    assert "not already recorded" in second


def test_a_failed_bell_write_does_not_fail_the_webhook(client, db, invoice, monkeypatch):
    """The reversal is committed before the bell. A bell that cannot be written
    must not 500 the event — Stripe would redeliver a reversal already done."""
    def boom(*a, **k):
        raise RuntimeError("bell down")

    monkeypatch.setattr("gdx_dispatch.core.office_notifications.notify_office", boom)
    _succeeded(client, invoice)
    assert _deliver(client, "charge.failed", {"id": CHARGE, "object": "charge", "payment_intent": INTENT})["status"] == "reversed"
    db.expire_all()
    assert db.get(Invoice, invoice.id).status == "sent"


# ── the customer's receipt ──────────────────────────────────────────────────


def test_a_processor_payment_queues_exactly_one_receipt(client, db, invoice, receipts):
    _succeeded(client, invoice)
    _succeeded(client, invoice)  # redelivery: no new money, no second receipt
    assert receipts == [str(invoice.id)]


def _receipt_rows(db, invoice):
    from gdx_dispatch.core.audit import AuditLog

    db.expire_all()
    emails = db.scalars(select(OutboundEmail).where(OutboundEmail.entity_id == str(invoice.id))).all()
    audits = db.scalars(select(AuditLog).where(
        AuditLog.entity_id == str(invoice.id), AuditLog.action.like("payment_receipt_auto%")
    )).all()
    return emails, audits


def test_receipt_is_really_sent_for_a_paid_invoice(client, db, invoice, monkeypatch):
    """Real invocation: the real prep, the real transactional layer, the real
    outbound_emails row. Only the wire (SMTP) and the PDF renderer are stubbed."""
    from gdx_dispatch.routers.invoices import send_automatic_payment_receipt

    wire: list[dict] = []
    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email._try_smtp",
        lambda **kw: wire.append(kw) or (True, None),
    )
    monkeypatch.setattr("gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **kw: b"%PDF-1.4 tiny")
    _succeeded(client, invoice)

    out = send_automatic_payment_receipt(db, str(invoice.id), reference=INTENT)

    assert out["sent"] is True, out
    emails, audits = _receipt_rows(db, invoice)
    assert [(e.kind, e.status, e.to_email, e.initiator_kind) for e in emails] == [
        ("receipt", "sent", "pat@example.com", "auto-receipt")
    ]
    assert [a.action for a in audits] == ["payment_receipt_auto"]
    assert audits[0].user_id == "auto-receipt"
    assert wire[0]["subject"].startswith("Payment received")
    assert wire[0]["attachments"][0]["name"].endswith("-paid.pdf")

    # A second payment seconds later does not send a second receipt.
    again = send_automatic_payment_receipt(db, str(invoice.id), reference="pi_other")
    assert again == {"sent": False, "skip_reason": "duplicate_send_suppressed"}


def test_receipt_goes_out_as_the_automation_mailbox(client, db, invoice, monkeypatch):
    """A tenant that mails through Outlook and has no SMTP still delivers the
    receipt: it is sent as Settings → Automation email's "Send as" mailbox.
    SMTP is NOT stubbed here; it is made to fail, so only Outlook can pass."""
    from gdx_dispatch.models.tenant_models import AppSettings
    from gdx_dispatch.routers.invoices import send_automatic_payment_receipt

    sender = str(uuid4())
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings()
        db.add(row)
    row.automation_sender_user_id = sender
    db.commit()

    graph: list[dict] = []
    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email._try_outlook_graph",
        lambda **kw: graph.append(kw) or (True, None),
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email._try_smtp",
        lambda **kw: (False, "smtp_not_configured"),
    )
    monkeypatch.setattr("gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **kw: b"%PDF-1.4 tiny")
    _succeeded(client, invoice)

    out = send_automatic_payment_receipt(db, str(invoice.id), reference=INTENT)

    assert out["sent"] is True and out["provider"] == "outlook_graph", out
    assert [str(g["user_id"]) for g in graph] == [sender]
    emails, _audits = _receipt_rows(db, invoice)
    assert [(e.status, e.provider) for e in emails] == [("sent", "outlook_graph")]


def test_no_receipt_for_a_partial_payment(db, invoice):
    from gdx_dispatch.routers.invoices import send_automatic_payment_receipt

    out = send_automatic_payment_receipt(db, str(invoice.id), reference=INTENT)
    assert out == {"sent": False, "skip_reason": "invoice_not_paid"}
    emails, audits = _receipt_rows(db, invoice)
    assert emails == [] and audits == []


def test_receipt_with_no_email_on_file_is_on_the_activity_trail(client, db, invoice, customer):
    from gdx_dispatch.routers.invoices import get_invoice_activity, send_automatic_payment_receipt

    customer.email = None
    db.commit()
    _succeeded(client, invoice)

    out = send_automatic_payment_receipt(db, str(invoice.id), reference=INTENT)
    assert out["sent"] is False and out["skip_reason"] == "customer_has_no_email"

    act = get_invoice_activity(invoice_id=invoice.id, _={"user_id": "u"}, db=db, limit=50)
    labels = [i["label"] for i in act["items"]]
    assert "Automatic receipt not sent — no customer email on file" in labels


def test_failed_delivery_is_recorded_not_claimed(client, db, invoice, monkeypatch):
    """No mail provider configured: the attempt is recorded as not sent."""
    from gdx_dispatch.routers.invoices import send_automatic_payment_receipt

    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email._try_smtp",
        lambda **kw: (False, "smtp_not_configured"),
    )
    monkeypatch.setattr("gdx_dispatch.core.pdf_generator.generate_invoice_pdf", lambda **kw: b"%PDF-1.4 tiny")
    _succeeded(client, invoice)

    out = send_automatic_payment_receipt(db, str(invoice.id), reference=INTENT)

    assert out["sent"] is False
    emails, audits = _receipt_rows(db, invoice)
    assert len(emails) == 1 and emails[0].kind == "receipt"
    assert emails[0].status != "sent"
    assert [a.action for a in audits] == ["payment_receipt_auto"]
    assert audits[0].details["email_sent"] is False


# ── the office can see a bank transfer that is still moving ────────────────


def _pending(db):
    from gdx_dispatch.routers.invoices import list_ach_pending

    return list_ach_pending(_={"user_id": "u"}, db=db)


def _intent(invoice_id, status, amount, **extra):
    return MagicMock(id=f"pi_{uuid4().hex[:8]}", status=status, amount=amount,
                     metadata={"invoice_id": str(invoice_id)}, **extra)


def test_pending_bank_transfer_is_listed_through_the_real_scan(db, invoice, monkeypatch):
    """The REAL `_ach_in_flight` over the REAL paged scan: one list call
    answers for every invoice; another invoice's intent and a card intent
    waiting on 3DS are not bank transfers on this one."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    calls = []
    data = [
        _intent(uuid4(), "processing", 1),
        _intent(invoice.id, "processing", 12_345),
    ]

    def lister(**kw):
        calls.append(kw)
        return MagicMock(data=data, has_more=False)

    monkeypatch.setattr("stripe.PaymentIntent.list", lister)
    out = _pending(db)
    assert out == {"checked": True, "pending": {str(invoice.id): {"stage": "processing", "amount": 123.45}}}
    assert len(calls) == 1


def test_micro_deposit_wait_reads_as_verifying_and_3ds_does_not(db, invoice, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    card_3ds = _intent(invoice.id, "requires_action", 500, next_action={"type": "use_stripe_sdk"})
    monkeypatch.setattr("stripe.PaymentIntent.list", lambda **kw: MagicMock(data=[card_3ds], has_more=False))
    assert _pending(db)["pending"] == {}

    bank = _intent(invoice.id, "requires_action", 50_000, next_action={
        "type": "verify_with_microdeposits", "verify_with_microdeposits": {"hosted_verification_url": "x"},
    })
    monkeypatch.setattr("stripe.PaymentIntent.list", lambda **kw: MagicMock(data=[bank], has_more=False))
    assert _pending(db)["pending"] == {str(invoice.id): {"stage": "verifying", "amount": 500.0}}


def test_stripe_outage_says_unchecked_not_none_pending(db, invoice, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")

    def down(**kw):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr("stripe.PaymentIntent.list", down)
    assert _pending(db) == {"checked": False, "pending": {}}


def test_no_stripe_key_skips_the_scan_and_settled_invoices_are_not_pending(db, invoice, monkeypatch):
    calls = []

    def lister(**kw):
        calls.append(kw)
        return MagicMock(data=[_intent(invoice.id, "processing", 100)], has_more=False)

    monkeypatch.setattr("stripe.PaymentIntent.list", lister)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert _pending(db) == {"checked": True, "pending": {}}
    assert calls == []

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    transition_invoice_status(db, invoice, "void")
    db.commit()
    assert _pending(db) == {"checked": True, "pending": {}}


def test_ach_pending_route_is_gated_and_not_swallowed_by_the_id_route():
    """`/ach-pending` must be matched before `/{invoice_id}` (which would 422
    on a non-UUID) and carry the Billing list's own permission."""
    from fastapi.routing import APIRoute

    from gdx_dispatch.routers.invoices import router

    paths = [r.path for r in router.routes if isinstance(r, APIRoute) and "GET" in r.methods]
    pending = next(p for p in paths if p.endswith("/ach-pending"))
    by_id = next(p for p in paths if p.endswith("/{invoice_id}"))
    assert paths.index(pending) < paths.index(by_id)
    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == pending)
    gates = [getattr(d.dependency, "__qualname__", "") for d in route.dependencies]
    assert any("require_permission" in g for g in gates), gates


# ── the trail ───────────────────────────────────────────────────────────────


def test_bank_transfer_lifecycle_is_on_the_activity_trail(client, db, invoice, monkeypatch):
    from gdx_dispatch.routers.invoices import get_invoice_activity

    _deliver(client, "payment_intent.processing", {
        "id": INTENT, "object": "payment_intent", "status": "processing", "amount": CENTS,
        "payment_method_types": ["us_bank_account"], "metadata": {"invoice_id": str(invoice.id)},
    })
    _intent_failed(client, invoice)

    act = get_invoice_activity(invoice_id=invoice.id, _={"user_id": "u"}, db=db, limit=50)
    labels = [i["label"] for i in act["items"]]
    assert "Bank transfer started — pending (up to 4 business days)" in labels
    assert "Bank transfer FAILED — the balance is still owed" in labels
