"""
gdx_dispatch/tests/test_16_integrations.py — Integration system tests (Zapier-style webhooks).

10 tests covering: create, list, update, delete, fire_event delivery, event filtering,
multi-config dispatch, HMAC signing, Zapier subscribe pattern, and delivery history lookup.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db():
    """Isolated SQLite in-memory DB with only the tables needed for integration tests."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.integrations import IntegrationConfig
    from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create only the tables this module depends on — avoids FK resolution errors
    # from unrelated models that share TenantBase.
    for tbl in [
        AuditLog.__table__,
        AIAction.__table__,
        WebhookEndpoint.__table__,
        WebhookDelivery.__table__,
        IntegrationConfig.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    yield db
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_integration(fresh_db):
    """Creating an IntegrationConfig row persists and is queryable."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-001",
        integration_type="custom_webhook",
        name="My Webhook",
        webhook_url="https://example.com/hook",
        secret="mysecret",
        events=[TriggerEvent.job_created.value],
        is_active=True,
    )
    db.add(config)
    db.commit()

    result = db.execute(
        select(IntegrationConfig).where(IntegrationConfig.tenant_id == "tenant-001")
    ).scalars().all()
    assert len(result) == 1
    assert result[0].name == "My Webhook"


def test_list_integrations(fresh_db):
    """Two configs for the same tenant are both returned."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent

    db = fresh_db
    for i in range(2):
        db.add(IntegrationConfig(
            tenant_id="tenant-002",
            integration_type="zapier",
            name=f"Config {i}",
            webhook_url=f"https://example.com/hook{i}",
            secret="s",
            events=[TriggerEvent.invoice_paid.value],
        ))
    db.commit()

    results = db.execute(
        select(IntegrationConfig).where(IntegrationConfig.tenant_id == "tenant-002")
    ).scalars().all()
    assert len(results) == 2


def test_update_integration(fresh_db):
    """Updating a config's name and events persists correctly."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-003",
        integration_type="slack",
        name="Old Name",
        webhook_url="https://hooks.slack.com/test",
        secret="s",
        events=[TriggerEvent.job_created.value],
    )
    db.add(config)
    db.commit()

    config.name = "New Name"
    config.events = [TriggerEvent.job_completed.value]
    db.commit()

    updated = db.get(type(config), config.id)
    assert updated.name == "New Name"
    assert TriggerEvent.job_completed.value in updated.events


def test_delete_integration(fresh_db):
    """Deleting a config removes it from the database."""
    from gdx_dispatch.core.integrations import IntegrationConfig

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-004",
        integration_type="custom_webhook",
        name="To Delete",
        webhook_url="https://example.com/del",
        secret="s",
        events=[],
    )
    db.add(config)
    db.commit()
    config_id = config.id

    db.delete(config)
    db.commit()

    assert db.get(IntegrationConfig, config_id) is None


def test_fire_event_creates_delivery(fresh_db):
    """fire_event creates a WebhookDelivery for a matching subscribed config."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent, fire_event
    from gdx_dispatch.core.webhooks.models import WebhookDelivery

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-005",
        integration_type="custom_webhook",
        name="Job Hook",
        webhook_url="https://example.com/job",
        secret="s",
        events=[TriggerEvent.job_created.value],
        is_active=True,
    )
    db.add(config)
    db.commit()

    payload = {"event": TriggerEvent.job_created.value, "job_id": "j1"}
    delivery_ids = fire_event("tenant-005", TriggerEvent.job_created.value, payload, db)

    assert len(delivery_ids) == 1
    delivery = db.get(WebhookDelivery, __import__("uuid").UUID(delivery_ids[0]))
    assert delivery is not None
    assert delivery.event_type == TriggerEvent.job_created.value


def test_fire_event_filters_events(fresh_db):
    """fire_event returns no deliveries when the config is not subscribed to the event."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent, fire_event

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-006",
        integration_type="custom_webhook",
        name="Job Only Hook",
        webhook_url="https://example.com/jobonly",
        secret="s",
        events=[TriggerEvent.job_created.value],
        is_active=True,
    )
    db.add(config)
    db.commit()

    payload = {"event": TriggerEvent.invoice_paid.value}
    delivery_ids = fire_event("tenant-006", TriggerEvent.invoice_paid.value, payload, db)

    assert delivery_ids == []


def test_fire_event_multiple_configs(fresh_db):
    """fire_event dispatches to all configs subscribed to the triggered event."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent, fire_event

    db = fresh_db
    for i in range(2):
        db.add(IntegrationConfig(
            tenant_id="tenant-007",
            integration_type="custom_webhook",
            name=f"Hook {i}",
            webhook_url=f"https://example.com/multi{i}",
            secret="s",
            events=[TriggerEvent.job_created.value],
            is_active=True,
        ))
    db.commit()

    payload = {"event": TriggerEvent.job_created.value}
    delivery_ids = fire_event("tenant-007", TriggerEvent.job_created.value, payload, db)

    assert len(delivery_ids) == 2


def test_hmac_signing_correct(fresh_db):
    """sign_payload returns a properly formatted sha256 HMAC signature."""
    from gdx_dispatch.core.webhooks.delivery import sign_payload

    sig = sign_payload(b'{"test": 1}', "mysecret")
    assert sig.startswith("sha256=")
    assert len(sig) == len("sha256=") + 64  # sha256 hex is 64 chars


def test_zapier_subscribe_config(fresh_db):
    """A zapier-type IntegrationConfig is stored with the correct integration_type."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-008",
        integration_type="zapier",
        name="Zapier: job.created",
        webhook_url="https://hooks.zapier.com/abc123",
        secret="zapsecret",
        events=[TriggerEvent.job_created.value],
        is_active=True,
    )
    db.add(config)
    db.commit()

    result = db.get(IntegrationConfig, config.id)
    assert result.integration_type == "zapier"
    assert TriggerEvent.job_created.value in result.events


def test_delivery_history_via_endpoint(fresh_db):
    """After fire_event, deliveries can be queried via the associated WebhookEndpoint."""
    from gdx_dispatch.core.integrations import IntegrationConfig, TriggerEvent, fire_event
    from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

    db = fresh_db
    config = IntegrationConfig(
        tenant_id="tenant-009",
        integration_type="custom_webhook",
        name="History Hook",
        webhook_url="https://example.com/history",
        secret="s",
        events=[TriggerEvent.customer_created.value],
        is_active=True,
    )
    db.add(config)
    db.commit()

    payload = {"event": TriggerEvent.customer_created.value, "customer_id": "c1"}
    delivery_ids = fire_event("tenant-009", TriggerEvent.customer_created.value, payload, db)
    assert len(delivery_ids) == 1

    # Look up endpoint by URL then query deliveries
    endpoint = db.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.url == config.webhook_url)
    ).scalars().first()
    assert endpoint is not None

    deliveries = db.execute(
        select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
    ).scalars().all()
    assert len(deliveries) == 1
    assert deliveries[0].event_type == TriggerEvent.customer_created.value
