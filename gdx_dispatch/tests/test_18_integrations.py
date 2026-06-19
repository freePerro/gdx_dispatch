"""
test_18_integrations.py — Zapier REST Hooks + Native Webhook management tests.

Tests:
  1. test_zapier_subscribe        — POST /integrations/zapier/subscribe creates WebhookEndpoint
  2. test_zapier_unsubscribe      — DELETE /integrations/zapier/unsubscribe deactivates endpoint
  3. test_webhook_registration    — POST /webhooks creates endpoint with events + secret_hint
  4. test_webhook_delivery        — delivery history returns correct records per endpoint
  5. test_zapier_test_event       — POST /integrations/zapier/test builds + sends (mocked) test payload
"""
from __future__ import annotations

import json
import secrets
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def integrations_db():
    """Isolated in-memory DB with only webhook tables (no FK cross-table issues)."""
    from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create only the tables we need — avoids FK resolution failures from other modules
    for tbl in [AIAction.__table__, WebhookEndpoint.__table__, WebhookDelivery.__table__]:
        tbl.create(bind=engine, checkfirst=True)

    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. test_zapier_subscribe
# ---------------------------------------------------------------------------

def test_zapier_subscribe(integrations_db):
    """Subscribing creates an active WebhookEndpoint with the given hook_url and event_type."""
    from sqlalchemy import select

    from gdx_dispatch.core.webhooks.models import WebhookEndpoint
    from gdx_dispatch.integrations.zapier import ZapierSubscribe, zapier_subscribe

    hook_url = "https://hooks.zapier.com/hooks/catch/12345/abcdef/"
    event_type = "job.created"

    body = ZapierSubscribe(hook_url=hook_url, event_type=event_type)

    # Build a minimal mock request with request.state.tenant
    mock_request = MagicMock()
    mock_request.state.tenant = {"id": str(uuid.uuid4())}

    response = zapier_subscribe(body, mock_request, integrations_db)
    data = json.loads(response.body)

    assert response.status_code in (200, 201), f"Unexpected status: {response.status_code} — {data}"
    assert data["hook_url"] == hook_url
    assert data["event_type"] == event_type
    assert "id" in data

    # Verify the endpoint was persisted
    ep = integrations_db.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.url == hook_url)
    ).scalars().first()
    assert ep is not None
    assert ep.is_active is True
    assert event_type in ep.events


# ---------------------------------------------------------------------------
# 2. test_zapier_unsubscribe
# ---------------------------------------------------------------------------

def test_zapier_unsubscribe(integrations_db):
    """Unsubscribing deactivates the WebhookEndpoint by hook_url."""
    from sqlalchemy import select

    from gdx_dispatch.core.webhooks.models import WebhookEndpoint
    from gdx_dispatch.integrations.zapier import ZapierUnsubscribe, zapier_unsubscribe

    # Pre-create an active subscription
    hook_url = "https://hooks.zapier.com/hooks/catch/99999/xxxxxx/"
    ep = WebhookEndpoint(
        url=hook_url,
        secret=secrets.token_hex(32),
        events=["invoice.paid"],
        is_active=True,
    )
    integrations_db.add(ep)
    integrations_db.commit()

    body = ZapierUnsubscribe(hook_url=hook_url)
    response = zapier_unsubscribe(body, integrations_db)
    data = json.loads(response.body)

    assert response.status_code == 200
    assert data["status"] == "unsubscribed"

    # Verify deactivated
    integrations_db.expire_all()
    ep_refreshed = integrations_db.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.url == hook_url)
    ).scalars().first()
    assert ep_refreshed is not None
    assert ep_refreshed.is_active is False


# ---------------------------------------------------------------------------
# 3. test_webhook_registration
# ---------------------------------------------------------------------------

def test_webhook_registration(integrations_db):
    """POST /webhooks registers an endpoint and returns secret_hint."""
    from gdx_dispatch.integrations.native_webhooks import EndpointCreate, create_endpoint

    body = EndpointCreate(
        url="https://myapp.example.com/webhook",
        events=["job.created", "invoice.paid"],
        secret=None,  # should be auto-generated
    )

    response = create_endpoint(body, integrations_db)
    data = json.loads(response.body)

    assert response.status_code == 201, f"Unexpected status: {response.status_code} — {data}"
    assert data["url"] == "https://myapp.example.com/webhook"
    assert "job.created" in data["events"]
    assert "invoice.paid" in data["events"]
    assert "secret_hint" in data
    assert len(data["secret_hint"]) == 6
    assert "id" in data


def test_webhook_registration_invalid_event(integrations_db):
    """POST /webhooks with an unsupported event returns 400."""
    from gdx_dispatch.integrations.native_webhooks import EndpointCreate, create_endpoint

    body = EndpointCreate(
        url="https://myapp.example.com/webhook",
        events=["not.a.real.event"],
    )
    response = create_endpoint(body, integrations_db)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 4. test_webhook_delivery
# ---------------------------------------------------------------------------

def test_webhook_delivery(integrations_db):
    """Delivery history endpoint returns correct records for a specific endpoint."""
    from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint
    from gdx_dispatch.integrations.native_webhooks import list_deliveries

    # Create endpoint
    ep = WebhookEndpoint(
        url="https://deliver-test.example.com/hook",
        secret=secrets.token_hex(32),
        events=["job.completed"],
        is_active=True,
    )
    integrations_db.add(ep)
    integrations_db.flush()

    # Create 3 deliveries for this endpoint
    for i in range(3):
        d = WebhookDelivery(
            endpoint_id=ep.id,
            event_type="job.completed",
            payload={"job_id": f"job-{i}"},
            idempotency_key=f"test:job.completed:job-{i}:{ep.id}",
            status="delivered",
            attempt_count=1,
            response_status=200,
            company_id="tenant-test",
        )
        integrations_db.add(d)

    # Create 1 delivery for a different endpoint (should not appear)
    other_ep = WebhookEndpoint(
        url="https://other.example.com/hook",
        secret=secrets.token_hex(32),
        events=["invoice.paid"],
        is_active=True,
    )
    integrations_db.add(other_ep)
    integrations_db.flush()
    other_d = WebhookDelivery(
        endpoint_id=other_ep.id,
        event_type="invoice.paid",
        payload={"invoice_id": "inv-999"},
        idempotency_key=f"test:invoice.paid:inv-999:{other_ep.id}",
        status="delivered",
        attempt_count=1,
        response_status=200,
        company_id="tenant-test",
    )
    integrations_db.add(other_d)
    integrations_db.commit()

    response = list_deliveries(str(ep.id), integrations_db)
    data = json.loads(response.body)

    assert response.status_code == 200
    assert len(data) == 3
    assert all(d["event_type"] == "job.completed" for d in data)


# ---------------------------------------------------------------------------
# 5. test_zapier_test_event
# ---------------------------------------------------------------------------

def test_zapier_test_event(integrations_db):
    """POST /integrations/zapier/test sends test payload and returns response_status."""
    from gdx_dispatch.integrations.zapier import ZapierTest, zapier_test

    body = ZapierTest(
        hook_url="https://hooks.zapier.com/hooks/catch/12345/testonly/",
        event_type="customer.created",
    )

    mock_request = MagicMock()
    mock_request.state.tenant = {"id": str(uuid.uuid4())}

    # Patch _post_url so no real HTTP call is made
    with patch("gdx_dispatch.integrations.zapier._post_url", return_value=200) as mock_post:
        response = zapier_test(body, mock_request, integrations_db)
        data = json.loads(response.body)

    assert response.status_code == 200
    assert data["status"] == "sent"
    assert data["response_status"] == 200
    assert mock_post.called

    # Verify the call was made with the correct hook_url
    call_args = mock_post.call_args
    assert call_args[0][0] == body.hook_url
    # Payload should contain the event type
    payload_bytes = call_args[0][1]
    payload_dict = json.loads(payload_bytes)
    assert payload_dict["event"] == "customer.created"
    assert payload_dict["test"] is True
