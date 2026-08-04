"""Tests for gdx_dispatch/core/payments.py — Stripe Elements + ACH payment collection.

Tests use unittest.mock to patch stripe API calls so no live Stripe key is
required. An isolated SQLite in-memory tenant DB is used for invoice fixtures.

A large share of these are ATTACK tests. These endpoints serve the anonymous
/pay/{token} page, so authorization is structural rather than session-based:
the token says which invoice you may touch, and the server decides the amount,
the intent↔invoice binding and the ACH method↔invoice binding. Each of the
"attack" tests below corresponds to something that was exploitable before
2026-08-04 — keep them.
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
# ACH
# ---------------------------------------------------------------------------


def _si(sid="seti_test", *, invoice_id=None, pm="pm_bank_123"):
    m = MagicMock()
    m.id = sid
    m.client_secret = f"{sid}_secret"
    m.payment_method = pm
    m.metadata = {"invoice_id": str(invoice_id)} if invoice_id else {}
    return m


def test_ach_setup_requires_invoice_target(client):
    """ATTACK: unlimited unauthenticated SetupIntent minting. ach/setup used to
    take only an email, with no invoice at all."""
    resp = client.post("/api/payments/ach/setup", json={"customer_email": "c@example.com"})
    assert resp.status_code == 422


def test_ach_setup_binds_invoice_and_returns_id(client, invoice):
    with patch("stripe.SetupIntent.create", return_value=_si()) as mock_create:
        resp = client.post(
            "/api/payments/ach/setup",
            json={"customer_email": "c@example.com", "invoice_token": TOKEN},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_secret"] == "seti_test_secret"
    assert body["setup_intent_id"] == "seti_test"
    assert mock_create.call_args[1]["metadata"]["invoice_id"] == str(invoice.id)


def test_ach_charge_requires_setup_intent(client, invoice):
    """Fail closed when the binding proof is absent (e.g. a pre-deploy tab)."""
    resp = client.post(
        "/api/payments/ach/charge",
        json={"payment_method_id": "pm_bank_123", "invoice_token": TOKEN},
    )
    assert resp.status_code == 409
    assert "refresh" in resp.json()["detail"].lower()


def test_ach_charge_rejects_setup_intent_for_another_invoice(client, invoice, other_invoice):
    """ATTACK: use a SetupIntent minted for invoice B to charge invoice A."""
    with patch("stripe.SetupIntent.retrieve", return_value=_si(invoice_id=other_invoice.id)), \
         patch("stripe.PaymentIntent.create") as mock_charge:
        resp = client.post(
            "/api/payments/ach/charge",
            json={
                "payment_method_id": "pm_bank_123",
                "setup_intent_id": "seti_test",
                "invoice_token": TOKEN,
            },
        )

    assert resp.status_code == 409, resp.text
    mock_charge.assert_not_called()


def test_ach_charge_rejects_unbound_payment_method(client, invoice):
    """ATTACK (the unauthorized-debit one): charge an arbitrary saved bank
    account whose pm_ id leaked. The SetupIntent names a DIFFERENT payment
    method, so the requested one was never collected for this invoice."""
    with patch("stripe.SetupIntent.retrieve", return_value=_si(invoice_id=invoice.id, pm="pm_someone_else")), \
         patch("stripe.PaymentIntent.create") as mock_charge:
        resp = client.post(
            "/api/payments/ach/charge",
            json={
                "payment_method_id": "pm_victims_bank",
                "setup_intent_id": "seti_test",
                "invoice_token": TOKEN,
            },
        )

    assert resp.status_code == 409, resp.text
    mock_charge.assert_not_called()


def test_ach_charge_success_uses_server_amount(client, invoice):
    with patch("stripe.SetupIntent.retrieve", return_value=_si(invoice_id=invoice.id)), \
         patch("stripe.PaymentIntent.create", return_value=_pi(status="processing")) as mock_charge:
        resp = client.post(
            "/api/payments/ach/charge",
            json={
                "payment_method_id": "pm_bank_123",
                "setup_intent_id": "seti_test",
                "invoice_token": TOKEN,
                "amount": 1,  # ignored
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "processing"
    assert mock_charge.call_args[1]["amount"] == 16200


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

    with patch("stripe.SetupIntent.retrieve", return_value=_si(invoice_id=invoice.id)), \
         patch("stripe.PaymentIntent.create", return_value=_pi(status="processing")) as mock_ach:
        client.post(
            "/api/payments/ach/charge",
            json={
                "payment_method_id": "pm_bank_123",
                "setup_intent_id": "seti_test",
                "invoice_token": TOKEN,
            },
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
