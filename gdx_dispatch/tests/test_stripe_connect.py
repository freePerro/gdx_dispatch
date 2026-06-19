from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.routers import stripe_connect as stripe_connect_router


class FakeControlDB:
    def __init__(self, tenant: object | None = None) -> None:
        self.tenant = tenant
        self.committed = False

    def commit(self) -> None:
        self.committed = True


@pytest.fixture(autouse=True)
def _set_stripe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")


def _tenant_request(tenant_id: str = "tenant-1") -> object:
    return SimpleNamespace(state=SimpleNamespace(tenant={"id": tenant_id, "slug": "tenant-one"}))


def _webhook_request(payload: bytes, sig: str = "t=1,v1=sig") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/stripe/connect/webhook",
        "headers": [(b"stripe-signature", sig.encode())],
    }

    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        body_sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


def test_create_connected_account():
    from gdx_dispatch.core.stripe_connect import create_connected_account

    mock_account = MagicMock()
    mock_account.id = "acct_123"

    with patch("gdx_dispatch.core.stripe_connect.stripe.Account.create", return_value=mock_account) as mock_create:
        account = create_connected_account(
            tenant_name="Tenant A",
            email="owner@tenant-a.com",
            stripe_secret_key="sk_test_123",
        )

    assert account.id == "acct_123"
    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["email"] == "owner@tenant-a.com"



def test_create_account_link():
    from gdx_dispatch.core.stripe_connect import create_account_link

    mock_link = MagicMock()
    mock_link.url = "https://connect.stripe.test/link"

    with patch("gdx_dispatch.core.stripe_connect.stripe.AccountLink.create", return_value=mock_link) as mock_create:
        link = create_account_link(
            account_id="acct_123",
            return_url="https://app.example.com/return",
            refresh_url="https://app.example.com/refresh",
            stripe_secret_key="sk_test_123",
        )

    assert link.url.startswith("https://")
    mock_create.assert_called_once_with(
        account="acct_123",
        return_url="https://app.example.com/return",
        refresh_url="https://app.example.com/refresh",
        type="account_onboarding",
    )



def test_create_payment_intent():
    from gdx_dispatch.core.stripe_connect import create_payment_intent

    mock_pi = MagicMock()
    mock_pi.id = "pi_123"

    with patch("gdx_dispatch.core.stripe_connect.stripe.PaymentIntent.create", return_value=mock_pi) as mock_create:
        result = create_payment_intent(
            account_id="acct_123",
            amount_cents=5000,
            currency="usd",
            metadata={"order_id": "ord_1", "platform_fee_cents": 100},
            stripe_secret_key="sk_test_123",
        )

    assert result.id == "pi_123"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["amount"] == 5000
    assert kwargs["application_fee_amount"] == 100
    assert kwargs["transfer_data"]["destination"] == "acct_123"




def test_get_account_status():
    from gdx_dispatch.core.stripe_connect import get_account_status

    with patch(
        "gdx_dispatch.core.stripe_connect.stripe.Account.retrieve",
        return_value={
            "id": "acct_123",
            "charges_enabled": True,
            "payouts_enabled": True,
            "details_submitted": True,
        },
    ):
        status = get_account_status("acct_123", stripe_secret_key="sk_test_123")

    assert status["account_id"] == "acct_123"
    assert status["onboarding_complete"] is True


# ---------------------------------------------------------------------------
# Router tests (direct endpoint invocation)
# ---------------------------------------------------------------------------


def test_onboard_endpoint_requires_auth():
    onboard_route = next(
        r for r in stripe_connect_router.router.routes if getattr(r, "path", "") == "/api/stripe/connect/onboard"
    )
    dep_calls = [d.call for d in onboard_route.dependant.dependencies]
    assert get_current_user in dep_calls

    from types import SimpleNamespace
    fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(fake_request, token="not-a-valid-jwt"))
    assert exc.value.status_code == 401



def test_status_endpoint_returns_onboarding_status():
    request = _tenant_request()

    with patch("gdx_dispatch.routers.stripe_connect.get_account_status", return_value={"onboarding_complete": True}):
        response = stripe_connect_router.stripe_connect_status(
            request=request,
            account_id=None,
            _={"user_id": "user-1"},
            tenant_db=MagicMock(),
            control_db=FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id="acct_existing_1")),
        )

    assert response["onboarding_complete"] is True



def test_payment_intent_endpoint():
    request = _tenant_request()

    mock_pi = MagicMock()
    mock_pi.id = "pi_abc"
    mock_pi.client_secret = "pi_secret_abc"
    mock_pi.application_fee_amount = 100

    with patch("gdx_dispatch.routers.stripe_connect.create_payment_intent", return_value=mock_pi):
        response = stripe_connect_router.create_connect_payment_intent(
            body=stripe_connect_router.PaymentIntentRequest(
                account_id="acct_123",
                amount_cents=10000,
                currency="usd",
                fee_percent=1.0,
                metadata={"invoice_id": "inv_1"},
            ),
            request=request,
            _={"user_id": "user-1"},
            tenant_db=MagicMock(),
            control_db=FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id="acct_existing_1")),
        )

    assert response["payment_intent_id"] == "pi_abc"



def test_webhook_signature_verification():
    request = _webhook_request(b"{}", sig="t=1,v1=bad")

    with patch("gdx_dispatch.routers.stripe_connect.stripe.Webhook.construct_event", side_effect=Exception("bad sig")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(stripe_connect_router.stripe_connect_webhook(request))

    assert exc.value.status_code == 400



def test_webhook_account_updated():
    request = _webhook_request(b"{}")
    event = {
        "type": "account.updated",
        "data": {"object": {"id": "acct_123", "charges_enabled": True}},
    }

    with patch("gdx_dispatch.routers.stripe_connect.stripe.Webhook.construct_event", return_value=event):
        response = asyncio.run(stripe_connect_router.stripe_connect_webhook(request))

    assert response["status"] == "processed"
    assert response["event_type"] == "account.updated"



def test_webhook_payment_succeeded():
    request = _webhook_request(b"{}")
    event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_123", "amount": 1000}},
    }

    with patch("gdx_dispatch.routers.stripe_connect.stripe.Webhook.construct_event", return_value=event):
        response = asyncio.run(stripe_connect_router.stripe_connect_webhook(request))

    assert response["status"] == "processed"
    assert response["event_type"] == "payment_intent.succeeded"



def test_balance_endpoint():
    request = _tenant_request()

    with patch("gdx_dispatch.routers.stripe_connect.stripe.Balance.retrieve", return_value={"available": [{"amount": 3210}]}):
        response = stripe_connect_router.get_connect_balance(
            request=request,
            account_id=None,
            _={"user_id": "user-1"},
            tenant_db=MagicMock(),
            control_db=FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id="acct_existing_1")),
        )

    assert response["available"][0]["amount"] == 3210



def test_fee_included_in_payment():
    request = _tenant_request()

    mock_pi = MagicMock()
    mock_pi.id = "pi_fee"
    mock_pi.client_secret = "pi_secret_fee"
    mock_pi.application_fee_amount = 100

    with patch("gdx_dispatch.routers.stripe_connect.create_payment_intent", return_value=mock_pi) as mock_create:
        response = stripe_connect_router.create_connect_payment_intent(
            body=stripe_connect_router.PaymentIntentRequest(
                account_id="acct_123",
                amount_cents=10000,
                currency="usd",
                fee_percent=1.0,
                metadata={"invoice_id": "inv_2"},
            ),
            request=request,
            _={"user_id": "user-1"},
            tenant_db=MagicMock(),
            control_db=FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id="acct_existing_1")),
        )

    kwargs = mock_create.call_args.kwargs
    assert kwargs["metadata"]["platform_fee_cents"] == 100
    assert response["application_fee_amount"] == 100



def test_onboard_endpoint_success_updates_tenant_account():
    request = _tenant_request()
    fake_db = FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id=None))

    mock_acct = MagicMock()
    mock_acct.id = "acct_new_123"
    mock_link = MagicMock()
    mock_link.url = "https://connect.stripe.test/onboarding"

    with (
        patch("gdx_dispatch.routers.stripe_connect.create_connected_account", return_value=mock_acct),
        patch("gdx_dispatch.routers.stripe_connect.create_account_link", return_value=mock_link),
    ):
        response = stripe_connect_router.onboard_tenant(
            body=stripe_connect_router.OnboardRequest(
                tenant_name="Tenant A",
                email="owner@tenant-a.com",
                return_url="https://app.example.com/return",
                refresh_url="https://app.example.com/refresh",
            ),
            request=request,
            _={"user_id": "user-1"},
            tenant_db=MagicMock(),
            control_db=fake_db,
        )

    assert response["account_id"] == "acct_new_123"
    assert fake_db.tenant.stripe_connect_account_id == "acct_new_123"
    assert fake_db.committed is True
