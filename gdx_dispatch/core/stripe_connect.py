from __future__ import annotations

from typing import Any

import stripe


def _set_key(stripe_secret_key: str) -> None:
    stripe.api_key = stripe_secret_key


def create_connected_account(
    tenant_name: str,
    email: str,
    stripe_secret_key: str,
    account_type: str = "express",
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Create a Stripe Connect account for a tenant."""
    _set_key(stripe_secret_key)
    payload: dict[str, Any] = {
        "type": account_type,
        "email": email,
        "business_type": "company",
        "business_profile": {"name": tenant_name},
    }
    if metadata:
        payload["metadata"] = metadata
    return stripe.Account.create(
        **payload,
    )


def create_account_link(
    account_id: str,
    return_url: str,
    refresh_url: str,
    stripe_secret_key: str,
) -> Any:
    """Create an onboarding link for a connected account."""
    _set_key(stripe_secret_key)
    return stripe.AccountLink.create(
        account=account_id,
        return_url=return_url,
        refresh_url=refresh_url,
        type="account_onboarding",
    )


def create_payment_intent(
    account_id: str,
    amount_cents: int,
    currency: str,
    metadata: dict[str, Any] | None,
    stripe_secret_key: str,
) -> Any:
    """Create a destination-charge PaymentIntent for the connected account."""
    _set_key(stripe_secret_key)
    meta = metadata or {}
    fee = int(meta.get("platform_fee_cents", 0) or 0)

    payload: dict[str, Any] = {
        "amount": amount_cents,
        "currency": currency,
        "metadata": meta,
        "transfer_data": {"destination": account_id},
    }
    if fee > 0:
        payload["application_fee_amount"] = fee

    return stripe.PaymentIntent.create(**payload)


def get_account_status(account_id: str, stripe_secret_key: str) -> dict[str, Any]:
    """Return basic onboarding/activation status for a connected account."""
    _set_key(stripe_secret_key)
    acct = stripe.Account.retrieve(account_id)

    charges_enabled = bool(acct.get("charges_enabled", False))
    payouts_enabled = bool(acct.get("payouts_enabled", False))
    details_submitted = bool(acct.get("details_submitted", False))

    return {
        "account_id": account_id,
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
        "details_submitted": details_submitted,
        "onboarding_complete": charges_enabled and payouts_enabled and details_submitted,
    }


