"""
gdx_dispatch/tests/test_37_webhooks.py — Webhook delivery monitoring tests.

Tests cover:
 1. test_register_webhook_endpoint       — register_endpoint creates a row and returns correct data
 2. test_deliver_webhook_success         — deliver_webhook_event queues deliveries for subscribed endpoints
 3. test_deliver_webhook_failure_retry   — failed delivery increments attempt_count and sets next_retry_at
 4. test_dead_letter_queue               — get_dead_letter_queue returns only abandoned deliveries
 5. test_retry_delivery                  — retry_delivery resets status/count; raises on non-abandoned
 6. test_delivery_stats                  — get_delivery_stats returns correct counts and success_rate
 7. test_ping_endpoint                  — ping_endpointsends HTTP POST and returns status result
 8. test_webhook_tenant_isolated         — tenant_id scoping: stats reflect only rows in given DB

Uses isolated SQLite in-memory database — no external services required.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.webhook_delivery import (
    deliver_webhook_event,
    get_dead_letter_queue,
    get_delivery_stats,
    ping_endpoint,
    register_endpoint,
    retry_delivery,
)
from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = "tenant-webhook-test-001"
TENANT_B = "tenant-webhook-test-002"


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


# ---------------------------------------------------------------------------
# Test 1: register_endpoint
# ---------------------------------------------------------------------------


def test_register_webhook_endpoint(db):
    """register_endpoint creates a WebhookEndpoint row and returns metadata."""
    result = register_endpoint(
        tenant_id=TENANT_A,
        url="https://hooks.example.com/events",
        events=["job.created", "invoice.paid"],
        secret="mysecret",
        db=db,
    )

    assert result["created"] is True
    assert result["url"] == "https://hooks.example.com/events"
    assert set(result["events"]) == {"job.created", "invoice.paid"}
    assert "endpoint_id" in result

    # Verify row in DB
    # endpoint_id is returned as a string; SQLAlchemy UUID PK lookups require UUID.
    from uuid import UUID
    ep_row = db.get(WebhookEndpoint, UUID(result["endpoint_id"]))
    assert ep_row is not None
    assert ep_row.url == "https://hooks.example.com/events"
    assert ep_row.is_active is True


# ---------------------------------------------------------------------------
# Test 2: deliver_webhook_event — success path (inline, no Celery)
# ---------------------------------------------------------------------------


def test_deliver_webhook_success(db):
    """deliver_webhook_event queues a delivery for each subscribed active endpoint."""
    # Two endpoints: one subscribed to job.created, one not
    ep1 = _add_endpoint(db, events=["job.created", "job.updated"], url="https://a.example.com/hook")
    _add_endpoint(db, events=["invoice.paid"], url="https://b.example.com/hook")

    payload = {"id": str(uuid4()), "status": "open"}

    # Patch _emit_webhook to None so we exercise the inline fallback
    import gdx_dispatch.core.webhook_delivery as wdm
    original = wdm._emit_webhook
    wdm._emit_webhook = None
    try:
        count = deliver_webhook_event(TENANT_A, "job.created", payload, db)
    finally:
        wdm._emit_webhook = original

    # Only ep1 is subscribed to job.created
    assert count == 1
    deliveries = db.query(WebhookDelivery).all()
    assert len(deliveries) == 1
    assert deliveries[0].event_type == "job.created"
    assert deliveries[0].endpoint_id == ep1.id
    assert deliveries[0].status == "pending"


# ---------------------------------------------------------------------------
# Test 3: deliver_webhook_failure_retry — delivery attempt updates fields
# ---------------------------------------------------------------------------


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
    # After one failed attempt the status should be pending (retry scheduled) or abandoned
    assert delivery.status in ("pending", "abandoned")
    assert delivery.attempt_count >= 1
    assert delivery.last_attempt_at is not None
    assert delivery.response_status == 500


# ---------------------------------------------------------------------------
# Test 4: get_dead_letter_queue
# ---------------------------------------------------------------------------


def test_dead_letter_queue(db):
    """get_dead_letter_queue returns only 'abandoned' deliveries."""
    ep = _add_endpoint(db)
    abandoned1 = _add_delivery(db, ep.id, status="abandoned", attempt_count=8)
    abandoned2 = _add_delivery(db, ep.id, status="abandoned", attempt_count=8)
    _add_delivery(db, ep.id, status="delivered")
    _add_delivery(db, ep.id, status="pending")

    dlq = get_dead_letter_queue(TENANT_A, db)

    assert len(dlq) == 2
    ids = {d["id"] for d in dlq}
    assert str(abandoned1.id) in ids
    assert str(abandoned2.id) in ids
    assert all(d["status"] == "abandoned" for d in dlq)
    # All required fields present
    for d in dlq:
        assert "endpoint_id" in d
        assert "event_type" in d
        assert "attempt_count" in d
        assert "created_at" in d


# ---------------------------------------------------------------------------
# Test 5: retry_delivery
# ---------------------------------------------------------------------------


def test_retry_delivery(db):
    """retry_delivery resets abandoned delivery to pending with zeroed attempt_count."""
    ep = _add_endpoint(db)
    delivery = _add_delivery(db, ep.id, status="abandoned", attempt_count=8)

    result = retry_delivery(TENANT_A, str(delivery.id), db)

    assert result["queued"] is True
    assert result["delivery_id"] == str(delivery.id)

    db.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt_count == 0
    assert delivery.next_retry_at is None


def test_retry_delivery_rejects_non_abandoned(db):
    """retry_delivery raises ValueError when delivery is not in 'abandoned' state."""
    ep = _add_endpoint(db)
    delivery = _add_delivery(db, ep.id, status="delivered")

    with pytest.raises(ValueError, match="abandoned"):
        retry_delivery(TENANT_A, str(delivery.id), db)


# ---------------------------------------------------------------------------
# Test 6: get_delivery_stats
# ---------------------------------------------------------------------------


def test_delivery_stats(db):
    """get_delivery_stats returns correct aggregates and success_rate."""
    ep = _add_endpoint(db)
    _add_delivery(db, ep.id, status="delivered")
    _add_delivery(db, ep.id, status="delivered")
    _add_delivery(db, ep.id, status="delivered")
    _add_delivery(db, ep.id, status="failed")
    _add_delivery(db, ep.id, status="abandoned")
    _add_delivery(db, ep.id, status="pending")

    stats = get_delivery_stats(TENANT_A, db)

    assert stats["total"] == 6
    assert stats["delivered"] == 3
    assert stats["failed"] == 1
    assert stats["abandoned"] == 1
    assert stats["pending"] == 1
    assert abs(stats["success_rate"] - 0.5) < 0.01

    # Empty DB stats
    engine2 = _make_engine()
    Session2 = sessionmaker(bind=engine2, autoflush=False, autocommit=False)
    db2 = Session2()
    try:
        empty_stats = get_delivery_stats(TENANT_A, db2)
        assert empty_stats["total"] == 0
        assert empty_stats["success_rate"] == 0.0
    finally:
        db2.close()
        engine2.dispose()


# ---------------------------------------------------------------------------
# Test 7: test_endpoint
# ---------------------------------------------------------------------------


def test_test_endpoint_success(db):
    """ping_endpointreturns success=True when endpoint returns 2xx."""
    ep = _add_endpoint(db, secret="test-secret", url="https://hooks.example.com/test")

    with patch("gdx_dispatch.core.webhook_delivery.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = ping_endpoint(TENANT_A, str(ep.id), db)

    assert result["success"] is True
    assert result["status_code"] == 200
    assert result["endpoint_id"] == str(ep.id)


def test_test_endpoint_failure(db):
    """ping_endpointreturns success=False when endpoint returns 4xx/5xx."""
    from urllib.error import HTTPError

    ep = _add_endpoint(db, secret="test-secret", url="https://hooks.example.com/test")

    with patch("gdx_dispatch.core.webhook_delivery.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = HTTPError(
            url="https://hooks.example.com/test",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=None,
        )
        result = ping_endpoint(TENANT_A, str(ep.id), db)

    assert result["success"] is False
    assert result["status_code"] == 503


def test_test_endpoint_not_found(db):
    """ping_endpointraises ValueError for unknown endpoint_id."""
    with pytest.raises(ValueError, match="Endpoint not found"):
        ping_endpoint(TENANT_A, str(uuid4()), db)


# ---------------------------------------------------------------------------
# Test 8: Tenant isolation — stats reflect only current DB
# ---------------------------------------------------------------------------


def test_webhook_tenant_isolated():
    """Delivery stats are scoped to the tenant DB; separate DBs are fully isolated."""
    engine_a = _make_engine()
    engine_b = _make_engine()
    Session = sessionmaker(autoflush=False, autocommit=False)

    db_a = Session(bind=engine_a)
    db_b = Session(bind=engine_b)

    try:
        # Tenant A: 4 deliveries (3 delivered, 1 pending)
        ep_a = WebhookEndpoint(
            url="https://a.example.com/hook",
            secret="sec",
            events=["job.created"],
            is_active=True,
        )
        db_a.add(ep_a)
        db_a.commit()
        db_a.refresh(ep_a)
        for s in ["delivered", "delivered", "delivered", "pending"]:
            _add_delivery(db_a, ep_a.id, status=s, company_id=TENANT_A)

        # Tenant B: 1 delivery (abandoned)
        ep_b = WebhookEndpoint(
            url="https://b.example.com/hook",
            secret="sec",
            events=["job.created"],
            is_active=True,
        )
        db_b.add(ep_b)
        db_b.commit()
        db_b.refresh(ep_b)
        _add_delivery(db_b, ep_b.id, status="abandoned", company_id=TENANT_B)

        stats_a = get_delivery_stats(TENANT_A, db_a)
        stats_b = get_delivery_stats(TENANT_B, db_b)

        # Tenant A sees its 4 deliveries
        assert stats_a["total"] == 4
        assert stats_a["delivered"] == 3
        assert stats_a["abandoned"] == 0

        # Tenant B sees only its 1 abandoned delivery
        assert stats_b["total"] == 1
        assert stats_b["abandoned"] == 1
        assert stats_b["delivered"] == 0

        # DLQ: Tenant A has none, Tenant B has one
        dlq_a = get_dead_letter_queue(TENANT_A, db_a)
        dlq_b = get_dead_letter_queue(TENANT_B, db_b)
        assert len(dlq_a) == 0
        assert len(dlq_b) == 1

    finally:
        db_a.close()
        db_b.close()
        engine_a.dispose()
        engine_b.dispose()
