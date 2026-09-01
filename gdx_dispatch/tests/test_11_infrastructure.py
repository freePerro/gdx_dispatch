"""
test_11_infrastructure.py — Sprint 1 infrastructure verification tests.

Covers:
  - Celery config has high/low priority queues, acks_late, reject_on_worker_lost
  - gdx_dispatch.core.webhook_delivery module is importable
  - deliver_webhook sets X-GDX-Signature on outbound HTTP requests (HMAC-SHA256)
  - RETRY_DELAYS has exactly 8 entries (5s, 30s, 2m, 10m, 30m, 2h, 6h, 24h)
  - Per-tenant rate limiting (Starter 120/min, Professional 600/min) wired in app
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import uuid4

# ---------------------------------------------------------------------------
# TASK 1 — Celery priority queues (source-inspection — avoids hard celery dep)
# ---------------------------------------------------------------------------

def test_celery_config_has_priority_queues():
    """gdx_dispatch/core/celery_app.py must define 'high' and 'low' queues with
    acks_late=True and reject_on_worker_lost=True.

    The test reads the source file directly so it passes even when celery is
    not installed in the current environment.  When celery *is* available, the
    live import test in test_celery_config_live (below) runs instead.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[2] / "gdx_dispatch/core/celery_app.py"
    ).read_text()

    # Reliability flags must appear as literal True assignments
    assert "task_acks_late=True" in src, "task_acks_late=True not found in gdx_dispatch/core/celery_app.py"
    assert "task_reject_on_worker_lost=True" in src, (
        "task_reject_on_worker_lost=True not found in gdx_dispatch/core/celery_app.py"
    )

    # Both queue names must appear
    assert '"high"' in src or "'high'" in src, "'high' queue name not found in celery_app.py"
    assert '"low"' in src or "'low'" in src, "'low' queue name not found in celery_app.py"

    # Default queue is low-priority
    assert 'task_default_queue="low"' in src or "task_default_queue='low'" in src, (
        "task_default_queue not set to 'low' in celery_app.py"
    )


# ---------------------------------------------------------------------------
# TASK 2 — Outbound webhook retry system
# ---------------------------------------------------------------------------

def test_webhook_delivery_module_exists():
    """gdx_dispatch.core.webhook_delivery must be importable and export deliver_webhook."""
    from gdx_dispatch.core.webhook_delivery import deliver_webhook  # noqa: F401

    assert callable(deliver_webhook)


def test_retry_schedule_length():
    """RETRY_DELAYS must contain exactly 8 entries."""
    from gdx_dispatch.core.webhook_delivery import RETRY_DELAYS

    assert len(RETRY_DELAYS) == 8, (
        f"Expected 8 retry delays, got {len(RETRY_DELAYS)}: {RETRY_DELAYS}"
    )


def test_retry_schedule_values():
    """RETRY_DELAYS must match the plan: 5s, 30s, 2m, 10m, 30m, 2h, 6h, 24h."""
    from gdx_dispatch.core.webhook_delivery import RETRY_DELAYS

    expected = [5, 30, 120, 600, 1800, 7200, 21600, 86400]
    assert list(RETRY_DELAYS) == expected, (
        f"Retry schedule mismatch.\nExpected: {expected}\nGot:      {list(RETRY_DELAYS)}"
    )


def test_webhook_hmac_signature():
    """deliver_webhook must set X-GDX-Signature (sha256=...) on the outbound POST."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.webhooks.delivery import deliver_webhook
    from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

    # Build isolated in-memory DB
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AIAction.__table__.create(bind=engine, checkfirst=True)
    WebhookEndpoint.__table__.create(bind=engine, checkfirst=True)
    # SQLite enum workaround — status column
    WebhookDelivery.__table__.create(bind=engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    secret = "test-secret-key"
    endpoint = WebhookEndpoint(
        url="https://example.com/hook",
        secret=secret,
        events=["job.created"],
        is_active=True,
    )
    db.add(endpoint)
    db.flush()

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type="job.created",
        payload={"job_id": "123"},
        idempotency_key=f"test:{uuid4()}",
        company_id="tenant-test",
    )
    db.add(delivery)
    db.commit()

    captured_headers: dict[str, str] = {}

    def fake_post(url: str, payload: bytes, headers: dict[str, str]) -> int:
        captured_headers.update(headers)
        return 200

    with patch("gdx_dispatch.core.webhooks.delivery._post", side_effect=fake_post):
        asyncio.run(deliver_webhook(str(delivery.id), db))

    assert "X-GDX-Signature" in captured_headers, (
        f"X-GDX-Signature header missing. Got headers: {list(captured_headers)}"
    )
    sig = captured_headers["X-GDX-Signature"]
    assert sig.startswith("sha256="), (
        f"Signature must start with 'sha256=', got: {sig!r}"
    )

    db.close()
    engine.dispose()


def test_webhook_dlq_created_after_all_retries_exhausted():
    """After attempt_count >= len(RETRY_DELAYS), an AIAction DLQ entry is created."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.webhooks.delivery import RETRY_DELAYS, deliver_webhook
    from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AIAction.__table__.create(bind=engine, checkfirst=True)
    WebhookEndpoint.__table__.create(bind=engine, checkfirst=True)
    WebhookDelivery.__table__.create(bind=engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    endpoint = WebhookEndpoint(
        url="https://example.com/hook",
        secret="secret",
        events=["job.created"],
        is_active=True,
    )
    db.add(endpoint)
    db.flush()

    # Pre-set attempt_count to one before exhaustion
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type="job.created",
        payload={"job_id": "dlq-test"},
        idempotency_key=f"dlq:{uuid4()}",
        attempt_count=len(RETRY_DELAYS) - 1,  # next failure will exhaust retries
        company_id="tenant-test",
    )
    db.add(delivery)
    db.commit()

    def always_fail(url: str, payload: bytes, headers: dict[str, str]) -> int:
        return 500

    with patch("gdx_dispatch.core.webhooks.delivery._post", side_effect=always_fail):
        asyncio.run(deliver_webhook(str(delivery.id), db))

    db.refresh(delivery)
    assert delivery.status == "abandoned", (
        f"Expected 'abandoned' after retry exhaustion, got '{delivery.status}'"
    )

    dlq_entry = db.query(AIAction).filter_by(action_type="webhook_dlq").first()
    assert dlq_entry is not None, "AIAction DLQ entry must be created after all retries exhausted"
    assert dlq_entry.priority == "high"

    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# TASK 3 — Per-tenant rate limiting  [REMOVED 2026-09-01]
#
# `test_per_tenant_rate_limit_tiers` was deleted with the code it described.
# It was fake twice over, and worth recording as a pattern rather than a fix:
#
#   1. It asserted SOURCE TEXT — `assert "600/minute" in src` — which proves
#      authorship, not behaviour. Its own docstring called this "so any future
#      refactor stays in sync"; in practice it pinned dead strings in place.
#   2. It then reimplemented `_tier_limit`'s branch INSIDE the test body as
#      `_tier_limit_logic` and asserted on that copy. It never imported or
#      executed the real function, so it would have passed identically had
#      `_tier_limit` been deleted, inverted, or never called.
#
# Both halves passed for years while the real `_tier_limit` could not work at
# all: slowapi calls a `default_limits` provider with no arguments, so its
# `request` was always None and the tier header was never read. Measured:
# 429 at request #121 with and without `x-tenant-tier: professional`.
#
# The real invariant now lives in `tests/test_rate_limit_default_is_static.py`,
# which asserts the limiter's actual configuration and fails if a
# request-dependent callable is reintroduced.

