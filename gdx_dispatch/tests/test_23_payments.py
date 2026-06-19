"""
gdx_dispatch/tests/test_23_payments.py — Unit tests for Stripe payment processing.

All Stripe API calls are mocked — no real network calls are made.
Tests cover: PaymentIntent creation, saving a payment method, listing
payment methods, ACH bank account setup, and webhook processing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. test_payment_intent_creation
# ---------------------------------------------------------------------------

def test_payment_intent_creation():
    """create_payment_intent should call stripe.PaymentIntent.create with correct args."""
    mock_intent = MagicMock()
    mock_intent.id = "pi_test_001"
    mock_intent.client_secret = "pi_test_001_secret_abc"
    mock_intent.amount = 5000
    mock_intent.currency = "usd"
    mock_intent.status = "requires_payment_method"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent) as mock_create:
        from gdx_dispatch.core.stripe_payments import create_payment_intent

        result = create_payment_intent(
            amount_cents=5000,
            currency="usd",
            customer_id="cus_test123",
            metadata={"invoice_id": "inv_001"},
            stripe_secret_key="sk_test_fake",
        )

        mock_create.assert_called_once_with(
            amount=5000,
            currency="usd",
            customer="cus_test123",
            payment_method_types=["card"],
            metadata={"invoice_id": "inv_001"},
        )
        assert result.id == "pi_test_001"
        assert result.client_secret == "pi_test_001_secret_abc"
        assert result.amount == 5000


# ---------------------------------------------------------------------------
# 2. test_payment_method_save
# ---------------------------------------------------------------------------

def test_payment_method_save():
    """save_payment_method should attach the payment method to the customer."""
    mock_pm = MagicMock()
    mock_pm.id = "pm_test_card"
    mock_pm.type = "card"

    with patch("stripe.PaymentMethod.attach", return_value=mock_pm) as mock_attach:
        from gdx_dispatch.core.stripe_payments import save_payment_method

        result = save_payment_method(
            customer_id="cus_test123",
            payment_method_id="pm_test_card",
            stripe_secret_key="sk_test_fake",
        )

        mock_attach.assert_called_once_with("pm_test_card", customer="cus_test123")
        assert result.id == "pm_test_card"
        assert result.type == "card"


# ---------------------------------------------------------------------------
# 3. test_payment_method_list
# ---------------------------------------------------------------------------

def test_payment_method_list():
    """list_payment_methods should return the list of payment methods for a customer."""
    mock_card = MagicMock()
    mock_card.id = "pm_visa_4242"
    mock_card.type = "card"
    mock_card.card = MagicMock()
    mock_card.card.brand = "visa"
    mock_card.card.last4 = "4242"
    mock_card.card.exp_month = 12
    mock_card.card.exp_year = 2027

    mock_list_result = MagicMock()
    mock_list_result.data = [mock_card]

    with patch("stripe.PaymentMethod.list", return_value=mock_list_result) as mock_list:
        from gdx_dispatch.core.stripe_payments import list_payment_methods

        result = list_payment_methods(
            customer_id="cus_test123",
            pm_type="card",
            stripe_secret_key="sk_test_fake",
        )

        mock_list.assert_called_once_with(customer="cus_test123", type="card")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].id == "pm_visa_4242"
        assert result[0].card.brand == "visa"
        assert result[0].card.last4 == "4242"


# ---------------------------------------------------------------------------
# 4. test_ach_setup
# ---------------------------------------------------------------------------

def test_ach_setup():
    """create_ach_verification should create a bank token then attach it as a source."""
    mock_token = MagicMock()
    mock_token.id = "btok_test_bank"

    mock_source = MagicMock()
    mock_source.id = "ba_test_6789"
    mock_source.last4 = "6789"
    mock_source.status = "new"
    mock_source.bank_name = "Test Bank"

    with (
        patch("stripe.Token.create", return_value=mock_token) as mock_token_create,
        patch("stripe.Customer.create_source", return_value=mock_source) as mock_create_source,
    ):
        from gdx_dispatch.core.stripe_payments import create_ach_verification

        result = create_ach_verification(
            bank_name="Test Bank",
            routing="110000000",
            account="000123456789",
            customer_id="cus_test123",
            stripe_secret_key="sk_test_fake",
        )

        # Verify Token.create was called with correct bank account details
        mock_token_create.assert_called_once_with(
            bank_account={
                "country": "US",
                "currency": "usd",
                "account_holder_type": "individual",
                "routing_number": "110000000",
                "account_number": "000123456789",
                "bank_name": "Test Bank",
            }
        )
        # Verify Customer.create_source was called with the token
        mock_create_source.assert_called_once_with("cus_test123", source="btok_test_bank")

        assert result.id == "ba_test_6789"
        assert result.last4 == "6789"
        assert result.status == "new"


# ---------------------------------------------------------------------------
# 5. test_webhook_payment_succeeded
# ---------------------------------------------------------------------------

def test_webhook_payment_succeeded():
    """handle_webhook should parse payment_intent.succeeded and return structured result."""
    # Build a mock Stripe event dict-like object
    mock_event = {
        "id": "evt_test_001",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test_001",
                "amount": 5000,
                "currency": "usd",
                "customer": "cus_test123",
                "status": "succeeded",
            }
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event) as mock_construct:
        from gdx_dispatch.core.stripe_payments import handle_webhook

        result = handle_webhook(
            payload=b'{"type":"payment_intent.succeeded"}',
            sig_header="t=1234,v1=abcdef",
            webhook_secret="whsec_test_secret",
        )

        mock_construct.assert_called_once_with(
            payload=b'{"type":"payment_intent.succeeded"}',
            sig_header="t=1234,v1=abcdef",
            secret="whsec_test_secret",
        )
        assert result["event"] == "payment_intent.succeeded"
        assert result["payment_intent_id"] == "pi_test_001"
        assert result["amount"] == 5000
        assert result["status"] == "succeeded"
        assert result["customer"] == "cus_test123"


# ---------------------------------------------------------------------------
# Bonus: test_webhook_unknown_event_ignored
# ---------------------------------------------------------------------------

def test_webhook_unknown_event_ignored():
    """handle_webhook should return status=ignored for unrecognized event types."""
    mock_event = {
        "id": "evt_unknown_001",
        "type": "customer.created",
        "data": {"object": {"id": "cus_new_001"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        from gdx_dispatch.core.stripe_payments import handle_webhook

        result = handle_webhook(
            payload=b'{}',
            sig_header="t=1234,v1=abcdef",
            webhook_secret="whsec_test_secret",
        )

        assert result["event"] == "customer.created"
        assert result["status"] == "ignored"
