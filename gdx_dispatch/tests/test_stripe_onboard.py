from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from gdx_dispatch.routers import stripe_connect as stripe_connect_router


class FakeControlDB:
    def __init__(self, tenant: object | None = None) -> None:
        self.tenant = tenant
        self.committed = False

    def execute(self, _sql, _params=None):
        class _Row:
            def mappings(self):
                return self

            def first(self):
                return None

        return _Row()

    def commit(self) -> None:
        self.committed = True


class FakeTenantDB:
    def __init__(self, integrations: dict | None = None) -> None:
        self.integrations = integrations
        self.committed = False

    def execute(self, _sql, _params=None):
        value = self.integrations

        class _Scalar:
            def scalar_one_or_none(self_inner):
                return value

        return _Scalar()

    def commit(self) -> None:
        self.committed = True


def _tenant_request(tenant_id: str = "tenant-1") -> object:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": tenant_id, "slug": "tenant-one"}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


def _webhook_request(payload: bytes, sig: str = "t=1,v1=sig"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/stripe/connect/webhook",
        "headers": [(b"stripe-signature", sig.encode())],
    }

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(scope, receive)
    request.state.tenant = {"id": "tenant-1"}
    return request


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")


def test_onboard_creates_account_and_returns_url() -> None:
    request = _tenant_request()
    control_db = FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id=None))

    acct = MagicMock(id="acct_1")
    link = MagicMock(url="https://connect.example/onboard")

    with (
        patch("gdx_dispatch.routers.stripe_connect.create_connected_account", return_value=acct),
        patch("gdx_dispatch.routers.stripe_connect.create_account_link", return_value=link),
        patch("gdx_dispatch.routers.stripe_connect.log_audit_event_sync") as audit,
    ):
        out = stripe_connect_router.onboard_tenant(
            body=stripe_connect_router.OnboardRequest(
                tenant_name="T1",
                email="owner@example.com",
                return_url="https://app.example.com/return",
                refresh_url="https://app.example.com/refresh",
            ),
            request=request,
            _={"sub": "user-1"},
            tenant_db=FakeTenantDB(),
            control_db=control_db,
        )

    assert out["account_id"] == "acct_1"
    assert out["onboarding_url"].startswith("https://")
    assert control_db.tenant.stripe_connect_account_id == "acct_1"
    assert control_db.committed is True
    assert audit.called


def test_onboard_maps_value_error_to_400() -> None:
    request = _tenant_request()
    control_db = FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id=None))

    with patch("gdx_dispatch.routers.stripe_connect.create_connected_account", side_effect=ValueError("bad email")):
        with pytest.raises(HTTPException) as exc:
            stripe_connect_router.onboard_tenant(
                body=stripe_connect_router.OnboardRequest(
                    tenant_name="T1",
                    email="owner@example.com",
                    return_url="https://app.example.com/return",
                    refresh_url="https://app.example.com/refresh",
                ),
                request=request,
                _={"sub": "user-1"},
                tenant_db=FakeTenantDB(),
                control_db=control_db,
            )

    assert exc.value.status_code == 400


def test_onboard_maps_integrity_error_to_409() -> None:
    request = _tenant_request()
    control_db = FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id=None))
    err = IntegrityError("UPDATE tenants ...", {}, Exception("conflict"))

    with (
        patch("gdx_dispatch.routers.stripe_connect.create_connected_account", return_value=MagicMock(id="acct_1")),
        patch("gdx_dispatch.routers.stripe_connect.create_account_link", return_value=MagicMock(url="https://ok")),
        patch.object(control_db, "commit", side_effect=err),pytest.raises(HTTPException) as exc
    ):
        stripe_connect_router.onboard_tenant(
            body=stripe_connect_router.OnboardRequest(
                tenant_name="T1",
                email="owner@example.com",
                return_url="https://app.example.com/return",
                refresh_url="https://app.example.com/refresh",
            ),
            request=request,
            _={"sub": "user-1"},
            tenant_db=FakeTenantDB(),
            control_db=control_db,
        )

    assert exc.value.status_code == 409


def test_status_returns_account_state() -> None:
    request = _tenant_request()

    with (
        patch("gdx_dispatch.routers.stripe_connect.get_account_status", return_value={"onboarding_complete": True}) as status_call,
        patch("gdx_dispatch.routers.stripe_connect.log_audit_event_sync") as audit,
    ):
        out = stripe_connect_router.stripe_connect_status(
            request=request,
            account_id=None,
            _={"sub": "user-1"},
            tenant_db=FakeTenantDB(),
            control_db=FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id="acct_2")),
        )

    assert out["onboarding_complete"] is True
    status_call.assert_called_once()
    assert audit.called



def test_payment_intent_maps_value_error_to_400() -> None:
    request = _tenant_request()

    with patch("gdx_dispatch.routers.stripe_connect.create_payment_intent", side_effect=ValueError("bad amount")):
        with pytest.raises(HTTPException) as exc:
            stripe_connect_router.create_connect_payment_intent(
                body=stripe_connect_router.PaymentIntentRequest(account_id="acct_2", amount_cents=1000, currency="usd"),
                request=request,
                _={"sub": "user-1"},
                tenant_db=FakeTenantDB(),
                control_db=FakeControlDB(tenant=SimpleNamespace(stripe_connect_account_id="acct_2")),
            )

    assert exc.value.status_code == 400


def test_webhook_account_updated_triggers_control_db_update() -> None:
    request = _webhook_request(b"{}")
    event = {
        "type": "account.updated",
        "data": {"object": {"id": "acct_123", "charges_enabled": True, "payouts_enabled": True}},
    }

    with (
        patch("gdx_dispatch.routers.stripe_connect.stripe.Webhook.construct_event", return_value=event),
        patch("gdx_dispatch.routers.stripe_connect._update_tenant_connect_account") as updater,
    ):
        out = asyncio.run(stripe_connect_router.stripe_connect_webhook(request))

    assert out["status"] == "processed"
    updater.assert_called_once_with("acct_123")


def test_webhook_invalid_signature_returns_400_and_logs() -> None:
    request = _webhook_request(b"{}", sig="t=1,v1=bad")

    with (
        patch("gdx_dispatch.routers.stripe_connect.log.exception") as log_exception,
        patch("gdx_dispatch.routers.stripe_connect.stripe.Webhook.construct_event", side_effect=Exception("bad sig")),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(stripe_connect_router.stripe_connect_webhook(request))

    assert exc.value.status_code == 400
    assert log_exception.called
