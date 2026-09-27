"""
gdx_dispatch/core/stripe_payments.py — Stripe payment processing service layer.

Handles PaymentIntents, SetupIntents, listing saved payment methods and ACH
bank-account setup for GDX (single-tenant, self-hosted). Every function here is
called by `routers/payments.py`.

**Webhooks do not live here.** The one Stripe event sink is
`routers/stripe_webhook.py`, which verifies the signature and dispatches to
`core/payments.py::handle_payment_webhook` — the only handler that takes a
`Session`, so the only one that can record money. See
`tests/test_stripe_webhook_single_handler.py` for why this module must not grow
a second one.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import stripe

logger = logging.getLogger(__name__)


def _set_key(override: str | None) -> None:
    """Set the Stripe API key from override or environment variable."""
    stripe.api_key = override or os.getenv("STRIPE_SECRET_KEY", "")


def create_payment_intent(
    amount_cents: int,
    currency: str,
    customer_id: str,
    metadata: dict[str, Any] | None = None,
    stripe_secret_key: str | None = None,
) -> stripe.PaymentIntent:
    """
    Create a Stripe PaymentIntent for an immediate charge.

    Args:
        amount_cents: Amount in smallest currency unit (e.g. cents for USD).
        currency: ISO currency code (e.g. "usd").
        customer_id: Stripe customer ID (cus_xxx).
        metadata: Optional dict of metadata to attach to the PaymentIntent.
        stripe_secret_key: Override the STRIPE_SECRET_KEY env var.

    Returns:
        stripe.PaymentIntent with client_secret for frontend confirmation.

    Raises:
        stripe.error.StripeError: On any Stripe API error.
    """
    _set_key(stripe_secret_key)
    params: dict[str, Any] = {
        "amount": amount_cents,
        "currency": currency,
        "customer": customer_id,
        "payment_method_types": ["card"],
        "metadata": metadata or {},
    }
    intent = stripe.PaymentIntent.create(**params)
    logger.info("Created PaymentIntent %s for customer %s amount=%d %s", intent.id, customer_id, amount_cents, currency)
    return intent


def create_setup_intent(
    customer_id: str,
    stripe_secret_key: str | None = None,
) -> stripe.SetupIntent:
    """
    Create a Stripe SetupIntent for saving a payment method without charging.

    Args:
        customer_id: Stripe customer ID (cus_xxx).
        stripe_secret_key: Override the STRIPE_SECRET_KEY env var.

    Returns:
        stripe.SetupIntent with client_secret for frontend confirmation.

    Raises:
        stripe.error.StripeError: On any Stripe API error.
    """
    _set_key(stripe_secret_key)
    intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=["card"],
    )
    logger.info("Created SetupIntent %s for customer %s", intent.id, customer_id)
    return intent


def list_payment_methods(
    customer_id: str,
    pm_type: str = "card",
    stripe_secret_key: str | None = None,
) -> list[stripe.PaymentMethod]:
    """
    List all saved payment methods for a Stripe customer.

    Args:
        customer_id: Stripe customer ID (cus_xxx).
        pm_type: Payment method type to filter by (default: "card").
        stripe_secret_key: Override the STRIPE_SECRET_KEY env var.

    Returns:
        List of stripe.PaymentMethod objects.

    Raises:
        stripe.error.StripeError: On any Stripe API error.
    """
    _set_key(stripe_secret_key)
    result = stripe.PaymentMethod.list(customer=customer_id, type=pm_type)
    return list(result.data)


def charge_saved_method(
    customer_id: str,
    payment_method_id: str,
    amount_cents: int,
    currency: str = "usd",
    metadata: dict[str, Any] | None = None,
    stripe_secret_key: str | None = None,
    idempotency_key: str | None = None,
) -> stripe.PaymentIntent:
    """
    Charge a previously saved payment method off-session.

    Args:
        customer_id: Stripe customer ID (cus_xxx).
        payment_method_id: Stripe PaymentMethod ID (pm_xxx) to charge.
        amount_cents: Amount in smallest currency unit.
        currency: ISO currency code (default: "usd").
        metadata: Optional metadata dict.
        stripe_secret_key: Override the STRIPE_SECRET_KEY env var.
        idempotency_key: Stripe idempotency key. When supplied, a retried or
            double-submitted request with the same key is collapsed by Stripe
            into a single charge (prevents double-charge on network retry /
            double-click). Callers should derive one per logical charge attempt.

    Returns:
        Confirmed stripe.PaymentIntent.

    Raises:
        stripe.error.StripeError: On any Stripe API error.
    """
    _set_key(stripe_secret_key)
    _extra: dict[str, Any] = {}
    if idempotency_key:
        _extra["idempotency_key"] = idempotency_key
    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency=currency,
        customer=customer_id,
        payment_method=payment_method_id,
        off_session=True,
        confirm=True,
        metadata=metadata or {},
        **_extra,
    )
    logger.info(
        "Charged saved method %s for customer %s amount=%d %s intent=%s",
        payment_method_id,
        customer_id,
        amount_cents,
        currency,
        intent.id,
    )
    return intent


def create_ach_verification(
    bank_name: str,
    routing: str,
    account: str,
    customer_id: str,
    stripe_secret_key: str | None = None,
) -> Any:
    """
    Initiate ACH bank account setup by creating a bank account token and attaching
    it to the Stripe customer for micro-deposit verification.

    Args:
        bank_name: Human-readable bank name.
        routing: ABA routing number (9 digits).
        account: Bank account number.
        customer_id: Stripe customer ID (cus_xxx).
        stripe_secret_key: Override the STRIPE_SECRET_KEY env var.

    Returns:
        The created stripe.BankAccount (source) attached to the customer.

    Raises:
        stripe.error.StripeError: On any Stripe API error.
    """
    _set_key(stripe_secret_key)
    # Step 1: Create a bank account token
    token = stripe.Token.create(
        bank_account={
            "country": "US",
            "currency": "usd",
            "account_holder_type": "individual",
            "routing_number": routing,
            "account_number": account,
            "bank_name": bank_name,
        }
    )
    logger.info("Created bank account token %s for customer %s", token.id, customer_id)

    # Step 2: Attach token to customer as a source (bank account)
    source = stripe.Customer.create_source(customer_id, source=token.id)
    logger.info("Attached bank account source %s to customer %s", source.id, customer_id)
    return source
