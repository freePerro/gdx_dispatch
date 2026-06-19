"""Tests for gdx_dispatch/core/payments.py — Stripe Elements + ACH payment collection.

Tests use unittest.mock to patch stripe API calls so no live Stripe key is
required. An isolated SQLite in-memory tenant DB is used for invoice fixtures.
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
from gdx_dispatch.models.tenant_models import Invoice

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


@pytest.fixture
def invoice(db_session):
    """Create a test invoice in a draft/sent state."""
    inv = Invoice(
        customer_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        invoice_number="INV-TEST-001",
        billing_type="standard",
        subtotal=150.00,
        tax_amount=12.00,
        total=162.00,
        status="sent",
        public_token="test-public-token-abc123",
        company_id="tenant-test",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


# ---------------------------------------------------------------------------
# App fixture (isolated TestClient)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(db_session, invoice):
    """FastAPI TestClient with overridden DB dependency."""
    from fastapi import FastAPI

    from gdx_dispatch.core.payments import public_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(public_router)

    # Override tenant DB dependency
    from gdx_dispatch.core.database import get_db
    app.dependency_overrides[get_db] = lambda: db_session

    # Inject a fake tenant state
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    class FakeTenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            request.state.tenant = {"id": str(uuid.uuid4()), "stripe_connect_account_id": None}
            return await call_next(request)

    app.add_middleware(FakeTenantMiddleware)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Test 1: create_intent — happy path
# ---------------------------------------------------------------------------

def test_create_intent_success(client, invoice):
    """POST /api/payments/create-intent returns client_secret and payment_intent_id."""
    mock_pi = MagicMock()
    mock_pi.client_secret = "pi_test_secret_abc"
    mock_pi.id = "pi_test_123"

    with patch("stripe.PaymentIntent.create", return_value=mock_pi) as mock_create:
        resp = client.post(
            "/api/payments/create-intent",
            json={"invoice_id": str(invoice.id), "amount": 16200, "currency": "usd"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["client_secret"] == "pi_test_secret_abc"
    assert data["payment_intent_id"] == "pi_test_123"
    # Idempotency key was passed
    mock_create.call_args[1] if mock_create.call_args[1] else mock_create.call_args[0]
    assert "idempotency_key" in str(mock_create.call_args)


# ---------------------------------------------------------------------------
# Test 2: confirm_payment — marks invoice paid
# ---------------------------------------------------------------------------

def test_confirm_payment_marks_paid(client, db_session, invoice):
    """POST /api/payments/confirm sets invoice.status=paid when PI succeeded."""
    mock_pi = MagicMock()
    mock_pi.status = "succeeded"

    with patch("stripe.PaymentIntent.retrieve", return_value=mock_pi):
        resp = client.post(
            "/api/payments/confirm",
            json={"payment_intent_id": "pi_test_123", "invoice_id": str(invoice.id)},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "succeeded"

    db_session.refresh(invoice)
    assert invoice.status == "paid"
    assert invoice.paid_at is not None


# ---------------------------------------------------------------------------
# Test 3: ach_setup — returns client_secret
# ---------------------------------------------------------------------------

def test_ach_setup_returns_client_secret(client):
    """POST /api/payments/ach/setup returns SetupIntent client_secret."""
    mock_si = MagicMock()
    mock_si.client_secret = "seti_test_secret_xyz"

    with patch("stripe.SetupIntent.create", return_value=mock_si):
        resp = client.post(
            "/api/payments/ach/setup",
            json={"customer_email": "customer@example.com"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["client_secret"] == "seti_test_secret_xyz"


# ---------------------------------------------------------------------------
# Test 4: webhook payment_intent.succeeded — marks invoice paid
# ---------------------------------------------------------------------------

def test_webhook_payment_succeeded(db_session, invoice):
    """handle_payment_webhook marks invoice paid on payment_intent.succeeded."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "metadata": {"invoice_id": str(invoice.id)},
            }
        },
    }
    result = handle_payment_webhook(event, db_session)
    assert result["status"] == "paid"

    db_session.refresh(invoice)
    assert invoice.status == "paid"


# ---------------------------------------------------------------------------
# Test 5: webhook payment_intent.payment_failed — logs and returns failed
# ---------------------------------------------------------------------------

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

    # Invoice should NOT be marked paid
    db_session.refresh(invoice)
    assert invoice.status != "paid"


# ---------------------------------------------------------------------------
# Test 6: duplicate payment prevented (idempotency)
# ---------------------------------------------------------------------------

def test_idempotency_key_passed_to_stripe(client, invoice):
    """Stripe PaymentIntent.create is called with idempotency_key=gdx-pi-{id}."""
    mock_pi = MagicMock()
    mock_pi.client_secret = "pi_secret"
    mock_pi.id = "pi_idem_test"

    with patch("stripe.PaymentIntent.create", return_value=mock_pi) as mock_create:
        client.post(
            "/api/payments/create-intent",
            json={"invoice_id": str(invoice.id), "amount": 16200},
        )
        client.post(
            "/api/payments/create-intent",
            json={"invoice_id": str(invoice.id), "amount": 16200},
        )

    # Both calls use same idempotency key
    for call in mock_create.call_args_list:
        kwargs = call[1] if call[1] else {}
        assert kwargs.get("idempotency_key") == f"gdx-pi-{invoice.id}"


# ---------------------------------------------------------------------------
# Test 7: receipt email triggered (placeholder log check)
# ---------------------------------------------------------------------------

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
# Test 8: list payment methods
# ---------------------------------------------------------------------------

def test_list_payment_methods(client):
    """GET /api/payments/methods returns combined card + ACH methods list."""
    card_pm = MagicMock()
    card_pm.id = "pm_card_test"
    card_pm.type = "card"
    card_pm.card = MagicMock()
    card_pm.card.to_dict.return_value = {"brand": "visa", "last4": "4242"}
    card_pm.us_bank_account = None
    card_pm.created = 1700000000

    ach_pm = MagicMock()
    ach_pm.id = "pm_ach_test"
    ach_pm.type = "us_bank_account"
    ach_pm.card = None
    ach_pm.us_bank_account = MagicMock()
    ach_pm.us_bank_account.to_dict.return_value = {"bank_name": "Test Bank", "last4": "6789"}
    ach_pm.created = 1700000001

    card_page = MagicMock()
    card_page.data = [card_pm]
    ach_page = MagicMock()
    ach_page.data = [ach_pm]

    def _list_side_effect(**kwargs):
        if kwargs.get("type") == "card":
            return card_page
        return ach_page

    with patch("stripe.PaymentMethod.list", side_effect=_list_side_effect):
        resp = client.get("/api/payments/methods?customer_id=cus_test123")

    assert resp.status_code == 200, resp.text
    methods = resp.json()["methods"]
    assert len(methods) == 2
    ids = {m["id"] for m in methods}
    assert "pm_card_test" in ids
    assert "pm_ach_test" in ids
