"""Tests for gdx_dispatch/core/payments.py — Stripe Elements + ACH payment collection.

Tests use unittest.mock to patch stripe API calls so no live Stripe key is
required. An isolated SQLite in-memory tenant DB is used for invoice fixtures.

A large share of these are ATTACK tests. These endpoints serve the anonymous
/pay/{token} page, so authorization is structural rather than session-based:
the token says which invoice you may touch, and the server decides the amount
and the intent↔invoice binding (for ACH the bank account is bound to that same
intent by Stripe.js, in the browser). Each of the "attack" tests below
corresponds to something that was exploitable before 2026-08-04 — keep them.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, Payment

TOKEN = "test-public-token-abc123"
OTHER_TOKEN = "test-public-token-other999"

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


@pytest.fixture
def db_session():
    engine, Session = _make_db()
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _mk_invoice(db, *, token, number, total=162.00, balance=None, status="sent"):
    inv = Invoice(
        customer_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        invoice_number=number,
        billing_type="standard",
        subtotal=150.00,
        tax_amount=12.00,
        total=total,
        balance_due=total if balance is None else balance,
        status=status,
        public_token=token,
        company_id="tenant-test",
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@pytest.fixture
def invoice(db_session):
    """A sent invoice for $162.00 with the full amount still owed."""
    return _mk_invoice(db_session, token=TOKEN, number="INV-TEST-001")


@pytest.fixture
def other_invoice(db_session):
    """A second, unrelated invoice — the replay/cross-invoice target."""
    return _mk_invoice(db_session, token=OTHER_TOKEN, number="INV-TEST-002", total=5000.00)


# ---------------------------------------------------------------------------
# App fixture (isolated TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session):
    """FastAPI TestClient with overridden DB dependency."""
    from fastapi import FastAPI

    from gdx_dispatch.core.payments import public_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(public_router)

    from gdx_dispatch.core.database import get_db
    app.dependency_overrides[get_db] = lambda: db_session

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    class FakeTenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            request.state.tenant = {"id": str(uuid.uuid4()), "stripe_connect_account_id": None}
            return await call_next(request)

    app.add_middleware(FakeTenantMiddleware)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _pi(pid="pi_test_123", *, amount=16200, status="succeeded", invoice_id=None):
    m = MagicMock()
    m.id = pid
    m.amount = amount
    # M17.3: production reads amount_received (what MOVED). A bare MagicMock
    # auto-creates a truthy Mock for it and Mock/100 breaks recording — set it
    # the way a real succeeded intent carries it.
    m.amount_received = amount
    m.status = status
    m.client_secret = f"{pid}_secret"
    m.metadata = {"invoice_id": str(invoice_id)} if invoice_id else {}
    return m


# ---------------------------------------------------------------------------
# create-intent
# ---------------------------------------------------------------------------


def test_create_intent_success(client, invoice):
    """Token resolves the invoice; response carries the server-side amount."""
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_create:
        resp = client.post("/api/payments/create-intent", json={"invoice_token": TOKEN})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["client_secret"] == "pi_test_123_secret"
    assert data["payment_intent_id"] == "pi_test_123"
    assert data["amount"] == 16200
    kwargs = mock_create.call_args[1]
    assert kwargs["amount"] == 16200
    assert kwargs["metadata"]["invoice_id"] == str(invoice.id)


def test_create_intent_ignores_client_amount(client, invoice):
    """ATTACK: caller asks to pay 1 cent on a $162 invoice. Server charges the
    balance regardless — the client's amount is not an input to the charge."""
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_create:
        resp = client.post(
            "/api/payments/create-intent",
            json={"invoice_token": TOKEN, "amount": 1},
        )

    assert resp.status_code == 200, resp.text
    assert mock_create.call_args[1]["amount"] == 16200


def test_create_intent_charges_balance_not_total(client, db_session):
    """A partially paid invoice must be charged what it OWES.

    The pay page used to render (and send) invoice.total, so a customer who
    had already part-paid — or had a credit memo applied — was overcharged the
    full original total.
    """
    inv = _mk_invoice(db_session, token="tok-partial", number="INV-PART", total=162.00, balance=62.00)
    with patch("stripe.PaymentIntent.create", return_value=_pi(amount=6200)) as mock_create:
        resp = client.post("/api/payments/create-intent", json={"invoice_token": "tok-partial"})

    assert resp.status_code == 200, resp.text
    assert mock_create.call_args[1]["amount"] == 6200
    assert resp.json()["amount"] == 6200
    assert inv.id is not None


def test_a_replayed_intent_carrying_a_bank_account_is_cancelled_and_reminted(client, invoice):
    """A customer linked a bank and walked away at the mandate. Idempotency
    would replay that intent — secret included — to the next holder of the
    token, who could confirm it against the first person's account
    (2026-09-16 audit, round 3). Cancel it, mint fresh."""
    old = _pi("pi_old", status="requires_confirmation")
    old.payment_method = "pm_someones_bank"
    fresh = _pi("pi_fresh", status="requires_payment_method")
    fresh.payment_method = None
    with patch("stripe.PaymentIntent.create", side_effect=[old, fresh]) as create, \
            patch("stripe.PaymentIntent.retrieve", return_value=old), \
            patch("stripe.PaymentIntent.cancel") as cancel:
        resp = client.post("/api/payments/create-intent", json={"invoice_token": TOKEN, "method": "ach"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_intent_id"] == "pi_fresh"
    cancel.assert_called_once()
    assert cancel.call_args[0][0] == "pi_old"
    assert create.call_count == 2
    keys = [c[1]["idempotency_key"] for c in create.call_args_list]
    assert keys[0] != keys[1] and keys[1].startswith(keys[0])


def test_a_replayed_intent_with_no_account_yet_is_reused(client, invoice):
    """The customer merely reloaded before linking anything: same intent,
    nothing to cancel."""
    live = _pi("pi_same", status="requires_payment_method")
    live.payment_method = None
    with patch("stripe.PaymentIntent.create", return_value=live) as create, \
            patch("stripe.PaymentIntent.retrieve", return_value=live), \
            patch("stripe.PaymentIntent.cancel") as cancel:
        resp = client.post("/api/payments/create-intent", json={"invoice_token": TOKEN, "method": "ach"})
    assert resp.json()["payment_intent_id"] == "pi_same"
    cancel.assert_not_called()
    assert create.call_count == 1


def test_create_intent_unknown_token_404(client):
    resp = client.post("/api/payments/create-intent", json={"invoice_token": "nope-not-a-token"})
    assert resp.status_code == 404


def test_create_intent_requires_a_target(client):
    resp = client.post("/api/payments/create-intent", json={})
    assert resp.status_code == 422


def test_create_intent_void_invoice_409(client, db_session):
    _mk_invoice(db_session, token="tok-void", number="INV-VOID", status="void")
    resp = client.post("/api/payments/create-intent", json={"invoice_token": "tok-void"})
    assert resp.status_code == 409


def test_create_intent_zero_balance_409(client, db_session):
    _mk_invoice(db_session, token="tok-zero", number="INV-ZERO", balance=0.00)
    resp = client.post("/api/payments/create-intent", json={"invoice_token": "tok-zero"})
    assert resp.status_code == 409


def test_create_intent_legacy_shape_still_works(client, invoice):
    """Dual-accept: a /pay tab opened before the deploy sends invoice_id and
    must not 422 mid-payment. The amount is still server-derived."""
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_create:
        resp = client.post(
            "/api/payments/create-intent",
            json={"invoice_id": str(invoice.id), "amount": 1},
        )

    assert resp.status_code == 200, resp.text
    assert mock_create.call_args[1]["amount"] == 16200


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def test_confirm_payment_marks_paid(client, db_session, invoice):
    """POST /api/payments/confirm sets invoice.status=paid when PI succeeded."""
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(invoice_id=invoice.id)):
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_test_123", "invoice_token": TOKEN},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "succeeded"
    db_session.refresh(invoice)
    assert invoice.status == "paid"
    assert invoice.paid_at is not None


def test_confirm_labels_a_bank_intent_as_ach(client, db_session, invoice):
    """Stripe.js reports a bank debit as `processing`, so this branch is card
    in practice — but a us_bank_account intent that ever arrives here
    `succeeded` must not be booked as a card payment (2026-09-16 audit)."""
    pi = _pi(invoice_id=invoice.id)
    pi.payment_method_types = ["us_bank_account"]
    with patch("stripe.PaymentIntent.retrieve", return_value=pi):
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_test_123", "invoice_token": TOKEN},
        )
    assert resp.status_code == 200, resp.text
    pay = db_session.query(Payment).filter(Payment.invoice_id == invoice.id).one()
    assert pay.method == "ach"


def test_confirm_labels_from_the_payment_method_not_the_allowed_list(client, db_session, invoice):
    """Prod card intents are automatic-payment-method intents whose allowed
    list will grow `us_bank_account` the day the Dashboard's ACH toggle goes
    on. The rail is the PaymentMethod that paid, not the list (2026-09-16
    audit)."""
    from types import SimpleNamespace

    pi = _pi(invoice_id=invoice.id)
    pi.payment_method_types = ["card", "us_bank_account"]
    pi.payment_method = SimpleNamespace(id="pm_card_1", type="card")
    with patch("stripe.PaymentIntent.retrieve", return_value=pi) as retrieve:
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_test_123", "invoice_token": TOKEN},
        )
    assert resp.status_code == 200, resp.text
    assert retrieve.call_args[1].get("expand") == ["payment_method"]
    pay = db_session.query(Payment).filter(Payment.invoice_id == invoice.id).one()
    assert pay.method == "card"


def test_confirm_rejects_intent_for_a_different_invoice(client, db_session, invoice, other_invoice):
    """ATTACK (the headline one): replay a succeeded PaymentIntent against an
    unrelated invoice.

    Idempotency is keyed on (invoice_id, reference), so without this check one
    real payment could be re-credited once against every other invoice —
    settling invoices nobody paid for.
    """
    intent_for_other = _pi(pid="pi_belongs_to_other", amount=500000, invoice_id=other_invoice.id)
    with patch("stripe.PaymentIntent.retrieve", return_value=intent_for_other):
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_belongs_to_other", "invoice_token": TOKEN},
        )

    assert resp.status_code == 409, resp.text
    db_session.refresh(invoice)
    assert invoice.status != "paid"
    assert db_session.query(Payment).filter(Payment.invoice_id == invoice.id).count() == 0


def test_confirm_rejects_intent_with_no_metadata(client, db_session, invoice):
    """An intent created outside this flow carries no invoice binding."""
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(invoice_id=None)):
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_unbound", "invoice_token": TOKEN},
        )

    assert resp.status_code == 409
    db_session.refresh(invoice)
    assert invoice.status != "paid"


def test_confirm_not_succeeded_records_nothing(client, db_session, invoice):
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(status="processing", invoice_id=invoice.id)):
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_test_123", "invoice_token": TOKEN},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    db_session.refresh(invoice)
    assert invoice.status != "paid"


# ---------------------------------------------------------------------------
# ACH — one PaymentIntent, completed in the browser (2026-09-16)
# ---------------------------------------------------------------------------


def test_create_intent_ach_mints_a_us_bank_account_intent(client, invoice):
    """The bank rail is the same endpoint with ``method="ach"``. Stripe.js
    then attaches the linked account to THIS intent and records the mandate
    against it. The server must name the type: a default (card) intent is one
    collectBankAccountForPayment cannot attach a bank account to."""
    with patch("stripe.PaymentIntent.create", return_value=_pi(status="requires_payment_method")) as mock_create:
        resp = client.post(
            "/api/payments/create-intent", json={"invoice_token": TOKEN, "method": "ach"}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["method"] == "ach"
    kwargs = mock_create.call_args[1]
    assert kwargs["payment_method_types"] == ["us_bank_account"]
    assert kwargs["amount"] == 16200
    assert kwargs["metadata"]["invoice_id"] == str(invoice.id)
    assert kwargs["idempotency_key"] == f"gdx-pi-{invoice.id}-ach-16200"
    # No Customer and no server-side mandate: both live in the browser flow.
    assert "customer" not in kwargs
    assert "mandate_data" not in kwargs


def test_create_intent_card_is_the_default_and_names_no_bank_type(client, invoice):
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_create:
        resp = client.post("/api/payments/create-intent", json={"invoice_token": TOKEN})

    assert resp.status_code == 200, resp.text
    assert resp.json()["method"] == "card"
    assert "payment_method_types" not in mock_create.call_args[1]


def test_create_intent_rejects_an_unknown_method(client, invoice):
    with patch("stripe.PaymentIntent.create") as mock_create:
        resp = client.post(
            "/api/payments/create-intent", json={"invoice_token": TOKEN, "method": "wire"}
        )
    assert resp.status_code == 422
    mock_create.assert_not_called()


def test_ach_intent_ignores_the_client_amount_too(client, invoice):
    with patch("stripe.PaymentIntent.create", return_value=_pi(status="requires_payment_method")) as mock_create:
        client.post(
            "/api/payments/create-intent",
            json={"invoice_token": TOKEN, "method": "ach", "amount": 1},
        )
    assert mock_create.call_args[1]["amount"] == 16200


def test_the_old_ach_routes_are_gone(client):
    """``ach/setup`` + ``ach/charge`` minted a SetupIntent the page never
    confirmed, then charged its PaymentMethod on a second intent — which
    Stripe refuses without a Customer. No bank payment ever completed through
    them (reproduced in test mode 2026-09-16), so they were deleted, not
    patched."""
    assert client.post(
        "/api/payments/ach/setup",
        json={"customer_email": "c@example.com", "invoice_token": TOKEN},
    ).status_code == 404
    assert client.post(
        "/api/payments/ach/charge",
        json={"payment_method_id": "pm_x", "setup_intent_id": "seti_x", "invoice_token": TOKEN},
    ).status_code == 404


def test_the_pay_page_no_longer_ships_the_dead_ach_flow(client, invoice):
    """Absence assertions only — a present string proves nothing; the browser
    walk is the proof the new flow works."""
    with patch("stripe.PaymentIntent.list", return_value=MagicMock(data=[], has_more=False)):
        resp = client.get(f"/pay/{TOKEN}")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "/api/payments/ach/" not in html
    assert "auBankAccount" not in html
    assert "collectBankAccountForSetup" not in html


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def test_idempotency_key_includes_amount_and_method(client, invoice):
    """The key must vary with the amount, or Stripe rejects the retry for 24h
    once the balance changes ("same key, different params") and the customer
    cannot pay at all."""
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_create:
        client.post("/api/payments/create-intent", json={"invoice_token": TOKEN})

    key = mock_create.call_args[1]["idempotency_key"]
    assert key == f"gdx-pi-{invoice.id}-card-16200"


def test_idempotency_key_differs_when_balance_changes(client, db_session, invoice):
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_create:
        client.post("/api/payments/create-intent", json={"invoice_token": TOKEN})
        first = mock_create.call_args[1]["idempotency_key"]

        invoice.balance_due = 62.00
        db_session.commit()
        client.post("/api/payments/create-intent", json={"invoice_token": TOKEN})
        second = mock_create.call_args[1]["idempotency_key"]

    assert first != second


def test_card_and_ach_keys_do_not_collide(client, invoice):
    """A customer who tries card, fails, then switches to ACH must not be
    blocked by a key collision on the same invoice+amount."""
    with patch("stripe.PaymentIntent.create", return_value=_pi()) as mock_card:
        client.post("/api/payments/create-intent", json={"invoice_token": TOKEN})
    card_key = mock_card.call_args[1]["idempotency_key"]

    with patch("stripe.PaymentIntent.create", return_value=_pi(status="requires_payment_method")) as mock_ach:
        client.post(
            "/api/payments/create-intent", json={"invoice_token": TOKEN, "method": "ach"}
        )
    ach_key = mock_ach.call_args[1]["idempotency_key"]

    assert card_key != ach_key


# ---------------------------------------------------------------------------
# Removed endpoints
# ---------------------------------------------------------------------------


def test_unauthenticated_payment_method_endpoints_are_gone(client):
    """These leaked card last4/bank details for any Stripe customer id and let
    anyone detach a payment method. The authenticated portal router owns this
    surface; these duplicates were deleted."""
    assert client.get("/api/payments/methods?customer_id=cus_x").status_code == 404
    assert client.delete("/api/payments/methods/pm_x").status_code == 404


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_webhook_payment_succeeded(db_session, invoice):
    """handle_payment_webhook marks invoice paid on payment_intent.succeeded."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"metadata": {"invoice_id": str(invoice.id)}}},
    }
    result = handle_payment_webhook(event, db_session)
    assert result["status"] == "paid"

    db_session.refresh(invoice)
    assert invoice.status == "paid"


def test_webhook_labels_ach_payments_correctly(db_session, invoice):
    """ACH settles asynchronously, so the webhook — not /confirm — records most
    bank payments. Recording them as "card" misstates the books."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_ach_1",
                "metadata": {"invoice_id": str(invoice.id)},
                "payment_method_types": ["us_bank_account"],
                "amount_received": 16200,
            }
        },
    }
    assert handle_payment_webhook(event, db_session)["status"] == "paid"
    pay = db_session.query(Payment).filter(Payment.invoice_id == invoice.id).one()
    assert pay.method == "ach"


def test_webhook_labels_from_the_payment_method_when_the_list_is_ambiguous(db_session, invoice):
    """Same audit finding on the webhook: an allowed list naming both rails
    decides nothing; the PaymentMethod's type does. One retrieve, no more."""
    from types import SimpleNamespace

    from gdx_dispatch.core.payments import handle_payment_webhook

    def event(pid):
        return {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": pid,
                "metadata": {"invoice_id": str(invoice.id)},
                "payment_method_types": ["card", "us_bank_account"],
                "payment_method": f"pm_for_{pid}",
                "amount_received": 100,
            }},
        }

    with patch("stripe.PaymentMethod.retrieve", return_value=SimpleNamespace(type="card")) as r1:
        assert handle_payment_webhook(event("pi_amb_card"), db_session)["status"] == "paid"
    assert r1.call_count == 1
    with patch("stripe.PaymentMethod.retrieve", return_value=SimpleNamespace(type="us_bank_account")):
        assert handle_payment_webhook(event("pi_amb_bank"), db_session)["status"] == "paid"
    by_ref = {p.reference: p.method for p in db_session.query(Payment).filter(Payment.invoice_id == invoice.id)}
    assert by_ref == {"pi_amb_card": "card", "pi_amb_bank": "ach"}


def test_webhook_sets_the_stripe_key_before_reading_the_payment_method(db_session, invoice, monkeypatch):
    """After a deploy, a cold worker's first request can be the webhook for a
    debit that started four days earlier, and nothing upstream sets the key
    (2026-09-16 audit, round 3). Prove the key is set AT THE MOMENT the live
    read happens, not merely that the read was attempted."""
    from types import SimpleNamespace

    import stripe as stripe_mod

    from gdx_dispatch.core.payments import handle_payment_webhook

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_probe_key")
    seen = {}
    previous = stripe_mod.api_key
    stripe_mod.api_key = None
    try:
        def retrieve(pm, **kw):
            seen["key_at_call"] = stripe_mod.api_key
            return SimpleNamespace(type="us_bank_account")

        with patch("stripe.PaymentMethod.retrieve", side_effect=retrieve):
            out = handle_payment_webhook(
                {
                    "type": "payment_intent.succeeded",
                    "data": {"object": {
                        "id": "pi_cold", "metadata": {"invoice_id": str(invoice.id)},
                        "payment_method_types": ["card", "us_bank_account"],
                        "payment_method": "pm_cold", "amount_received": 100,
                    }},
                },
                db_session,
            )
    finally:
        stripe_mod.api_key = previous
    assert out["status"] == "paid"
    assert seen["key_at_call"] == "sk_test_probe_key"
    assert db_session.query(Payment).filter(Payment.reference == "pi_cold").one().method == "ach"


def test_webhook_refuses_to_guess_the_rail_on_a_money_event(db_session, invoice):
    """Ambiguous allowed list + unreadable PaymentMethod: raise, so the router
    500s and Stripe retries. A payment booked on the wrong rail is a wrong
    write; a retry is not."""
    from gdx_dispatch.core.payments import RailUndeterminable, handle_payment_webhook

    with patch("stripe.PaymentMethod.retrieve", side_effect=RuntimeError("no key")), \
            pytest.raises(RailUndeterminable):
        handle_payment_webhook(
            {
                "type": "payment_intent.succeeded",
                "data": {"object": {
                    "id": "pi_unknowable", "metadata": {"invoice_id": str(invoice.id)},
                    "payment_method_types": ["card", "us_bank_account"],
                    "payment_method": "pm_unreadable", "amount_received": 100,
                }},
            },
            db_session,
        )
    assert db_session.query(Payment).filter(Payment.reference == "pi_unknowable").count() == 0


def test_webhook_notes_a_failed_ach_on_the_trail_and_not_a_card_decline(db_session, invoice):
    """The end of the story: a bounced debit or a timed-out micro-deposit
    (2026-09-16 audit, round 4). Card declines stay off the trail — the
    customer sees those on screen, and they would flood it."""
    from gdx_dispatch.core.audit import AuditLog, ensure_audit_table
    from gdx_dispatch.core.payments import handle_payment_webhook

    ensure_audit_table(db_session)
    with patch("gdx_dispatch.core.payments._reverse_unless_superseded", return_value={"status": "no_payment_to_reverse"}):
        out = handle_payment_webhook(
            {
                "type": "payment_intent.payment_failed",
                "data": {"object": {
                    "id": "pi_bounced", "amount": 16200, "payment_method_types": ["us_bank_account"],
                    "metadata": {"invoice_id": str(invoice.id)},
                    "last_payment_error": {"code": "payment_method_microdeposit_verification_timeout", "message": "Microdeposit timeout."},
                }},
            },
            db_session,
        )
        assert out["status"] == "failed"
        row = db_session.query(AuditLog).filter(AuditLog.action == "ach_payment_failed").one()
        assert str(row.entity_id) == str(invoice.id)
        assert row.details["code"] == "payment_method_microdeposit_verification_timeout"
        assert row.details["reversal"] == "no_payment_to_reverse"

        handle_payment_webhook(
            {
                "type": "payment_intent.payment_failed",
                "data": {"object": {
                    "id": "pi_declined", "amount": 16200, "payment_method_types": ["card"],
                    "metadata": {"invoice_id": str(invoice.id)},
                    "last_payment_error": {"code": "card_declined", "message": "Card declined"},
                }},
            },
            db_session,
        )
    assert db_session.query(AuditLog).filter(AuditLog.action == "ach_payment_failed").count() == 1


def test_webhook_notes_a_microdeposit_wait_on_the_trail(db_session, invoice):
    """The micro-deposit wait can lock the pay page for 10 days; the office
    must be able to read why (2026-09-16 audit). Card 3DS waits are not it."""
    from gdx_dispatch.core.audit import AuditLog, ensure_audit_table
    from gdx_dispatch.core.payments import handle_payment_webhook

    ensure_audit_table(db_session)
    out = handle_payment_webhook(
        {
            "type": "payment_intent.requires_action",
            "data": {"object": {
                "id": "pi_md",
                "amount": 16200,
                "payment_method_types": ["us_bank_account"],
                "metadata": {"invoice_id": str(invoice.id)},
                "next_action": {
                    "type": "verify_with_microdeposits",
                    "verify_with_microdeposits": {"hosted_verification_url": "https://payments.stripe.com/verify/x", "arrival_date": 1}
                },
            }},
        },
        db_session,
    )
    assert out["status"] == "ach_verification_noted"
    row = db_session.query(AuditLog).filter(AuditLog.action == "ach_payment_awaiting_verification").one()
    assert str(row.entity_id) == str(invoice.id)
    assert row.details["intent_id"] == "pi_md"
    assert row.details["hosted_verification_url"] == "https://payments.stripe.com/verify/x"

    out = handle_payment_webhook(
        {
            "type": "payment_intent.requires_action",
            "data": {"object": {
                "id": "pi_3ds", "amount": 16200, "payment_method_types": ["card"],
                "metadata": {"invoice_id": str(invoice.id)},
                "next_action": {"type": "use_stripe_sdk"},
            }},
        },
        db_session,
    )
    assert out["status"] == "not_ach_verification"
    assert db_session.query(AuditLog).filter(AuditLog.action == "ach_payment_awaiting_verification").count() == 1


def test_webhook_payment_failed(db_session, invoice):
    """handle_payment_webhook returns failed status on payment_intent.payment_failed."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    event = {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "metadata": {"invoice_id": str(invoice.id)},
                "last_payment_error": {"message": "Card declined"},
            }
        },
    }
    result = handle_payment_webhook(event, db_session)
    assert result["status"] == "failed"
    assert result["invoice_id"] == str(invoice.id)
    assert "declined" in result.get("reason", "").lower()

    db_session.refresh(invoice)
    assert invoice.status != "paid"


def test_webhook_reverses_payment_on_refund(db_session, invoice):
    """Money can leave again. A refund/dispute/ACH-return must void the
    recorded payment and re-open the invoice — otherwise the books read paid,
    dunning stops chasing, and the cash is gone."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    paid = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_refund_me",
                "metadata": {"invoice_id": str(invoice.id)},
                "amount_received": 16200,
            }
        },
    }
    assert handle_payment_webhook(paid, db_session)["status"] == "paid"
    db_session.refresh(invoice)
    assert invoice.status == "paid"

    refund = {
        "type": "charge.refunded",
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_refund_me"}},
    }
    assert handle_payment_webhook(refund, db_session)["status"] == "reversed"

    db_session.refresh(invoice)
    assert invoice.status != "paid", "invoice must re-open after the money left"
    pay = db_session.query(Payment).filter(Payment.reference == "pi_refund_me").one()
    assert pay.voided_at is not None
    assert float(invoice.balance_due or 0) > 0


def test_webhook_reverses_on_ach_return(db_session, invoice):
    """An ACH debit can be returned days after it succeeded (R01/R10)."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_ach_return",
                    "metadata": {"invoice_id": str(invoice.id)},
                    "payment_method_types": ["us_bank_account"],
                    "amount_received": 16200,
                }
            },
        },
        db_session,
    )
    result = handle_payment_webhook(
        {
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_ach_return",
                    "metadata": {"invoice_id": str(invoice.id)},
                    "last_payment_error": {"message": "insufficient funds"},
                }
            },
        },
        db_session,
    )
    assert result["reversal"] == "reversed"
    db_session.refresh(invoice)
    assert invoice.status != "paid"


def test_webhook_reversal_without_recorded_payment_is_noop(db_session, invoice):
    """A reversal we never recorded a payment for is not an error."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    result = handle_payment_webhook(
        {"type": "charge.refunded", "data": {"object": {"payment_intent": "pi_unknown"}}},
        db_session,
    )
    assert result["status"] == "no_payment_to_reverse"


def test_webhook_raises_so_stripe_retries(db_session, invoice):
    """The handler must NOT swallow failures. Returning 200 on error tells
    Stripe the event was handled and it is never redelivered — a transient DB
    failure would silently lose a payment."""
    from gdx_dispatch.core import payments as payments_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("db exploded")

    original = payments_mod._mark_invoice_paid
    payments_mod._mark_invoice_paid = _boom
    try:
        with pytest.raises(RuntimeError):
            payments_mod.handle_payment_webhook(
                {
                    "type": "payment_intent.succeeded",
                    "data": {
                        "object": {
                            "id": "pi_x",
                            "metadata": {"invoice_id": str(invoice.id)},
                            "amount_received": 16200,
                        }
                    },
                },
                db_session,
            )
    finally:
        payments_mod._mark_invoice_paid = original


def test_receipt_email_placeholder_on_webhook(db_session, invoice, caplog):
    """handle_payment_webhook logs invoice paid — receipt hook point verified."""
    import logging

    from gdx_dispatch.core.payments import handle_payment_webhook

    event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"metadata": {"invoice_id": str(invoice.id)}}},
    }
    with caplog.at_level(logging.INFO, logger="gdx_dispatch.core.payments"):
        result = handle_payment_webhook(event, db_session)

    assert result["status"] == "paid"
    assert any(str(invoice.id) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# M3 — a partial refund must not void the whole payment
# ---------------------------------------------------------------------------


def _lined_invoice(db, *, number, unit_price):
    """An invoice with a REAL line.

    `_mk_invoice` creates none, and `_recalculate_invoice` derives the total
    from lines — so a line-less invoice is silently rewritten to just its
    preserved tax ($12). A balance assertion on that invoice holds no matter
    what the refund logic does, which is how the first draft of these tests
    "passed" while proving nothing.
    """
    from gdx_dispatch.models.tenant_models import InvoiceLine

    inv = _mk_invoice(db, token=uuid.uuid4().hex, number=number, total=unit_price)
    db.add(InvoiceLine(
        invoice_id=inv.id, description="Work", quantity=1,
        unit_price=unit_price, line_total=unit_price, taxable=False,
        company_id=inv.company_id,
    ))
    inv.tax_amount = 0
    db.commit()
    db.refresh(inv)
    return inv


def _pay(db, invoice, reference, cents):
    from gdx_dispatch.core.payments import handle_payment_webhook

    assert handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": reference,
                    "metadata": {"invoice_id": str(invoice.id)},
                    "amount_received": cents,
                }
            },
        },
        db,
    )["status"] == "paid"
    db.refresh(invoice)


def _refund_event(reference, charge_cents, refunded_cents, kind="charge.refunded"):
    return {
        "type": kind,
        "data": {
            "object": {
                "id": "ch_x",
                "payment_intent": reference,
                "amount": charge_cents,
                "amount_refunded": refunded_cents,
            }
        },
    }


def test_a_partial_refund_leaves_the_payment_and_the_invoice_alone(db_session):
    """The finding itself: refunding $50 of a $500 payment as goodwill used to
    void the entire $500 — balance back to $500, invoice flipped paid→sent, and
    dunning chasing a customer who had paid in full.

    Uses a LINED invoice paid at its exact total, so `balance_due == 0` is a
    real assertion about the refund rather than an artefact of a $12 invoice
    over-paid by $488.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    inv = _lined_invoice(db_session, number="INV-M3-PARTIAL", unit_price=500.00)
    assert float(inv.total) == 500.00, "the fixture must bill what we pay"
    _pay(db_session, inv, "pi_partial", 50000)
    assert inv.status == "paid"
    assert float(inv.balance_due or 0) == 0.0

    out = handle_payment_webhook(_refund_event("pi_partial", 50000, 5000), db_session)
    assert out["status"] == "partial_refund_not_recorded"
    assert out["refunded_total"] == 50.00

    db_session.refresh(inv)
    pay = db_session.query(Payment).filter(Payment.reference == "pi_partial").one()
    assert pay.voided_at is None, "a partial refund voided the whole payment"
    assert invoice_is_still_paid(inv), "the customer paid; the invoice must stay paid"


def invoice_is_still_paid(inv) -> bool:
    return inv.status == "paid" and float(inv.balance_due or 0) == 0.0


def test_the_partial_refund_is_recorded_as_a_fact_even_though_the_money_is_not(db_session):
    """It is not silently dropped. The office has to record the money entry
    (the endpoint that caps by net paid and posts to the ledger), so the event
    has to be findable — otherwise "we did not book it" becomes "nobody knew"."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.payments import handle_payment_webhook

    inv = _lined_invoice(db_session, number="INV-M3-AUDIT", unit_price=500.00)
    _pay(db_session, inv, "pi_audited", 50000)
    handle_payment_webhook(_refund_event("pi_audited", 50000, 5000), db_session)

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "stripe_partial_refund_received")
        .one()
    )
    assert row.details["refunded_total"] == 50.00
    assert row.details["charge_total"] == 500.00
    assert row.details["recorded"] is False
    assert row.user_id == "stripe:webhook"


def test_a_full_refund_still_voids_and_reopens(db_session):
    """The other half of `charge.refunded`, unchanged: the money really left."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    inv = _lined_invoice(db_session, number="INV-M3-FULL", unit_price=500.00)
    _pay(db_session, inv, "pi_full", 50000)
    out = handle_payment_webhook(_refund_event("pi_full", 50000, 50000), db_session)
    assert out["status"] == "reversed"

    db_session.refresh(inv)
    pay = db_session.query(Payment).filter(Payment.reference == "pi_full").one()
    assert pay.voided_at is not None
    assert inv.status != "paid"
    assert float(inv.balance_due or 0) > 0


def test_a_refund_without_amounts_falls_back_to_the_full_void(db_session):
    """Older payloads carry no `amount_refunded`. Absent is not zero, and must
    not be read as a partial refund of nothing."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    inv = _lined_invoice(db_session, number="INV-M3-NOAMT", unit_price=500.00)
    _pay(db_session, inv, "pi_noamt", 50000)
    out = handle_payment_webhook(
        {
            "type": "charge.refunded",
            "data": {"object": {"id": "ch_y", "payment_intent": "pi_noamt"}},
        },
        db_session,
    )
    assert out["status"] == "reversed"
    pay = db_session.query(Payment).filter(Payment.reference == "pi_noamt").one()
    assert pay.voided_at is not None


def test_a_dispute_still_voids_in_full_even_with_amounts_present(db_session):
    """Only `charge.refunded` splits. A dispute takes the whole payment back
    regardless of what the payload says about amounts.

    NOTE: a PARTIAL dispute has the same shape as the bug fixed here — a $50
    dispute on a $500 charge voids all $500. That is left alone deliberately:
    a dispute is provisional and needs the lifecycle M15 describes (there is no
    `charge.dispute.closed` handler at all), not a one-sided split.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    inv = _lined_invoice(db_session, number="INV-M3-DISP", unit_price=500.00)
    _pay(db_session, inv, "pi_disp", 50000)
    out = handle_payment_webhook(
        _refund_event("pi_disp", 50000, 5000, kind="charge.dispute.created"), db_session
    )
    assert out["status"] == "reversed"
    pay = db_session.query(Payment).filter(Payment.reference == "pi_disp").one()
    assert pay.voided_at is not None


# ---------------------------------------------------------------------------
# #422 — a payment landing after a void must not resurrect the invoice
#
# void_invoice releases the invoice's parts and change orders back to the
# unbilled checklist. A PaymentIntent that succeeded moments before the void,
# or a webhook redelivered after it, used to book its money and flip the
# invoice void -> paid: the books showed a PAID invoice whose work was
# simultaneously sitting unbilled. The office endpoint already 409'd this;
# the Stripe path had no guard at all.
# ---------------------------------------------------------------------------


def _voided_invoice(db):
    return _mk_invoice(
        db, token="tok_void_422", number="INV-VOID-422", status="void",
    )


def test_webhook_payment_on_a_voided_invoice_leaves_it_void(db_session):
    from gdx_dispatch.core.payments import handle_payment_webhook

    invoice = _voided_invoice(db_session)
    handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": "pi_void_422",
                "metadata": {"invoice_id": str(invoice.id)},
                "amount_received": 16200,
            }},
        },
        db_session,
    )
    db_session.refresh(invoice)
    assert invoice.status == "void", "a void is terminal — the payment must not resurrect it"
    assert not invoice.paid_at, "paid_at must not be stamped on an invoice that stayed void"


def test_the_money_is_still_recorded_so_it_can_be_refunded(db_session):
    """Refusing to record would strand cash at the processor with no local
    row to refund against — the same reasoning that makes an overcharge
    record in full rather than be discarded."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    invoice = _voided_invoice(db_session)
    handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": "pi_void_422b",
                "metadata": {"invoice_id": str(invoice.id)},
                "amount_received": 16200,
            }},
        },
        db_session,
    )
    rows = db_session.query(Payment).filter(Payment.invoice_id == invoice.id).all()
    assert len(rows) == 1, "the money moved; it must exist locally"
    assert float(rows[0].amount) == pytest.approx(162.00)
    assert rows[0].voided_at is None


def test_an_operator_visible_audit_row_is_written(db_session):
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.payments import handle_payment_webhook

    invoice = _voided_invoice(db_session)
    handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": "pi_void_422c",
                "metadata": {"invoice_id": str(invoice.id)},
                "amount_received": 16200,
            }},
        },
        db_session,
    )
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "payment_on_voided_invoice")
        .all()
    )
    assert rows, "money on a dead invoice must leave a searchable trail, not just a log line"
    assert str(invoice.id) in str(rows[0].entity_id)


# ---------------------------------------------------------------------------
# #661 — the overcharge audit row had never once been written
#
# _audit_overcharge wrapped log_audit_event_sync in db.begin_nested(). But
# _log_audit_event_impl calls ensure_audit_table on the way in, and THAT
# commits (SQLite) the first time it runs for an engine. The commit closed the
# savepoint's transaction, exiting the context manager raised
# InvalidRequestError, and the blanket except swallowed the row.
#
# The old guard test only asserted the PAYMENT survived a failing audit, which
# is true whether or not the row lands — so it stayed green for the whole life
# of the defect. These assert the row EXISTS.
# ---------------------------------------------------------------------------


def _overcharge(db, ref):
    """$500 charged against a $162 invoice."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    inv = _mk_invoice(db, token=f"tok_{ref}", number=f"INV-{ref.upper()}")
    handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": ref, "metadata": {"invoice_id": str(inv.id)},
                                "amount_received": 50000}},
        },
        db,
    )
    return inv


def test_the_overcharge_audit_row_actually_lands(db_session):
    from gdx_dispatch.core.audit import AuditLog

    inv = _overcharge(db_session, "pi_over_lands")
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "payment_exceeds_receivable")
        .all()
    )
    assert rows, (
        "the overcharge alert M12 built has to be searchable — a log line and a "
        "banner someone has to already be looking at is what it replaced"
    )
    assert str(inv.id) in str(rows[0].entity_id)


def test_the_row_lands_on_the_FIRST_audit_write_for_an_engine(db_session):
    """The exact condition that hid the bug.

    ensure_audit_table commits only once per engine. Every test here gets a
    fresh in-memory engine, so this write is always the first one — which is
    precisely when the savepoint used to be closed out from under the row.
    """
    from gdx_dispatch.core.audit import AuditLog

    assert db_session.query(AuditLog).count() == 0, "must be the first audit write"
    _overcharge(db_session, "pi_over_first")
    assert db_session.query(AuditLog).filter(
        AuditLog.action == "payment_exceeds_receivable"
    ).count() == 1


def test_the_overcharge_detail_survives_intact(db_session):
    from gdx_dispatch.core.audit import AuditLog

    _overcharge(db_session, "pi_over_detail")
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "payment_exceeds_receivable")
        .one()
    )
    assert float(row.details["charged"]) == pytest.approx(500.00)
    assert float(row.details["excess"]) == pytest.approx(338.00)
    assert row.user_id == "stripe-webhook"
