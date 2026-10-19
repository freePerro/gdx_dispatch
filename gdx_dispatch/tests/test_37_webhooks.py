"""
gdx_dispatch/tests/test_37_webhooks.py — webhook delivery retry test.

Covers the real delivery path, ``gdx_dispatch.core.webhooks.delivery``:
a failed HTTP attempt increments attempt_count and schedules a retry.

The tests for the dead ``core/webhook_delivery.py`` twin (its unguarded
``ping_endpoint`` and the service helpers nothing in the app called) went
with that module (GDXA-1678).

Uses isolated SQLite in-memory database — no external services required.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create only the webhook tables directly — avoid FK resolution errors
    # from unrelated models that share TenantBase.metadata.
    AIAction.__table__.create(bind=engine, checkfirst=True)
    WebhookEndpoint.__table__.create(bind=engine, checkfirst=True)
    WebhookDelivery.__table__.create(bind=engine, checkfirst=True)
    return engine


@pytest.fixture()
def db():
    engine = _make_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _add_endpoint(db, events=None, url="https://example.com/hook", secret="s3cr3t"):
    """Helper: insert a WebhookEndpoint directly."""
    ep = WebhookEndpoint(
        url=url,
        secret=secret,
        events=events or ["job.created"],
        is_active=True,
    )
    db.add(ep)
    db.commit()
    db.refresh(ep)
    return ep


def _add_delivery(db, endpoint_id, status="pending", attempt_count=0, event_type="job.created", company_id="tenant-webhook-test-001"):
    """Helper: insert a WebhookDelivery directly."""
    d = WebhookDelivery(
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload={"id": str(uuid4())},
        idempotency_key=f"test:{event_type}:{uuid4()}",
        status=status,
        attempt_count=attempt_count,
        company_id=company_id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def test_deliver_webhook_failure_retry(db):
    """After a failed HTTP delivery attempt, attempt_count increments and next_retry_at is set."""
    ep = _add_endpoint(db)
    delivery = _add_delivery(db, endpoint_id=ep.id, status="pending")

    # Simulate a failed delivery by calling the low-level deliver_webhook with a mocked HTTP error
    from unittest.mock import patch as mpatch

    delivery_id = str(delivery.id)

    async def _to_thread_noop(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        mpatch("gdx_dispatch.core.webhooks.delivery._post", return_value=500),
        mpatch("gdx_dispatch.core.webhooks.delivery.asyncio.to_thread", side_effect=_to_thread_noop),
    ):
        asyncio.run(
            __import__("gdx_dispatch.core.webhooks.delivery", fromlist=["deliver_webhook"]).deliver_webhook(
                delivery_id, db
            )
        )

    db.expire_all()
    db.refresh(delivery)
    # One failed attempt is well inside RETRY_DELAYS, so a retry must be scheduled.
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert delivery.next_retry_at is not None
    assert delivery.last_attempt_at is not None
    assert delivery.response_status == 500
