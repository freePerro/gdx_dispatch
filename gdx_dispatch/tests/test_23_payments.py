"""
gdx_dispatch/tests/test_23_payments.py — Unit tests for Stripe payment processing.

All Stripe API calls are mocked — no real network calls are made.
Tests cover the `core/stripe_payments.py` helpers that `routers/payments.py`
actually calls: PaymentIntent creation, listing payment methods, and ACH
bank-account setup.

Webhook handling is **not** tested here, and never was: the two webhook tests
removed on 2026-09-27 exercised a handler nothing called. The live path is
`routers/stripe_webhook.py` -> `core/payments.py::handle_payment_webhook`,
covered by `test_payments.py`; `test_stripe_webhook_single_handler.py` guards
against a second handler reappearing and holds the reasoning.
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
# 2. test_payment_method_list
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
# 3. test_ach_setup
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
