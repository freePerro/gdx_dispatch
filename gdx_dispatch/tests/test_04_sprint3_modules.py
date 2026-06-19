import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gdx_dispatch.core.terminology import DEFAULT_TERMINOLOGY, INDUSTRY_PRESETS
from gdx_dispatch.core.webhooks.delivery import RETRY_DELAYS, sign_payload
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.modules.campaigns.service import render_template
from gdx_dispatch.modules.change_orders.service import approve_change_order, create_change_order
from gdx_dispatch.modules.inventory.models import Part
from gdx_dispatch.modules.maintenance.models import ServicePlan
from gdx_dispatch.modules.maintenance.service import enroll_customer
from gdx_dispatch.modules.proposals.service import accept_tier, add_proposal_tier, create_estimate
from gdx_dispatch.modules.purchase_orders.service import create_po, receive_po
from gdx_dispatch.modules.workflows.engine import SUPPORTED_TRIGGERS, evaluate_conditions, fire_trigger


@pytest.fixture(autouse=True)
def _patch_apply_async(monkeypatch):
    monkeypatch.setattr("gdx_dispatch.modules.campaigns.service.send_campaign_task.apply_async", lambda *a, **k: None)


def test_webhook_signature_hmac():
    s1 = sign_payload(b"test payload", "mysecret")
    s2 = sign_payload(b"test payload", "mysecret")
    assert s1.startswith("sha256=") and s1 == s2


def test_webhook_retry_delays_count():
    assert len(RETRY_DELAYS) == 8 and all(v > 0 for v in RETRY_DELAYS)


def test_workflow_conditions_evaluation(tenant_db):
    rule = SimpleNamespace(conditions=[{"field": "lifecycle_stage", "operator": "eq", "value": "completed"}])
    assert "job.created" in SUPPORTED_TRIGGERS
    assert evaluate_conditions(rule, {"lifecycle_stage": "completed"}) is True
    assert evaluate_conditions(rule, {"lifecycle_stage": "scheduled"}) is False
    asyncio.run(fire_trigger("unsupported.event", {}, "tenant-1", tenant_db))


def test_maintenance_plan_enrollment(tenant_db):
    plan = ServicePlan(name="Gold", visits_per_year=2)
    cust = Customer(name="Bob", company_id="tenant-test")
    tenant_db.add_all([plan, cust]); tenant_db.commit(); tenant_db.refresh(plan); tenant_db.refresh(cust)  # noqa: E701,E702
    enrollment = enroll_customer(cust.id, plan.id, None, tenant_db)
    assert enrollment.id is not None and enrollment.next_service_at is not None


def test_purchase_order_receive_updates_inventory(tenant_db):
    part = Part(sku="SKU-PO-1", name="Bolt", qty_on_hand=5, reorder_point=1, unit_cost=1, unit_price=2)
    tenant_db.add(part); tenant_db.commit(); tenant_db.refresh(part)  # noqa: E701,E702
    po = create_po("Vendor", None, [{"part_id": part.id, "description": "Bolt", "qty": 3, "unit_cost": 1}], tenant_db)
    receive_po(po.id, tenant_db); tenant_db.refresh(part)  # noqa: E701,E702
    assert part.qty_on_hand == 8


def test_campaign_template_rendering():
    out = render_template("Hello {{customer_name}}, your estimate is {{estimate_total}}", {"customer_name": "Bob", "estimate_total": "$500"})
    assert out == "Hello Bob, your estimate is $500"


def test_change_order_lifecycle(tenant_db):
    job = Job(title="Test Job", company_id="tenant-test")
    tenant_db.add(job); tenant_db.commit(); tenant_db.refresh(job)  # noqa: E701,E702
    co = create_change_order(job.id, "Extra work", "Details", [], tenant_db)
    assert approve_change_order(co.id, "user-1", tenant_db).status == "approved"


def test_proposal_tier_accept(tenant_db):
    job = Job(title="Estimate Job", company_id="tenant-test")
    tenant_db.add(job); tenant_db.commit(); tenant_db.refresh(job)  # noqa: E701,E702
    est = create_estimate(job.id, "EST-1", tenant_db)
    tier = add_proposal_tier(est.id, "better", "desc", 500, 12, tenant_db)
    assert accept_tier(est.id, tier.id, tenant_db).status == "accepted"


def test_terminology_defaults():
    for key in ("job", "estimate", "invoice", "customer"):
        assert key in DEFAULT_TERMINOLOGY
    assert "garage_door" in INDUSTRY_PRESETS
    assert INDUSTRY_PRESETS["garage_door"]["job"] == "Service Call"


def test_terminology_preset_override():
    merged = {**DEFAULT_TERMINOLOGY, **INDUSTRY_PRESETS["garage_door"]}
    assert merged["job"] == "Service Call" and merged["invoice"] == "Invoice"


def test_webhook_dlq_on_abandon(tenant_db, monkeypatch):
    """When attempt_count exceeds RETRY_DELAYS length, deliver_webhook creates an AIAction DLQ entry."""
    from gdx_dispatch.core.webhooks.delivery import deliver_webhook
    from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

    # Patch _post to always return a 500 (failure)
    monkeypatch.setattr("gdx_dispatch.core.webhooks.delivery._post", lambda *a, **k: 500)

    endpoint = WebhookEndpoint(url="https://example.com/hook", secret="test-secret", events=["job.created"])
    tenant_db.add(endpoint)
    tenant_db.commit()
    tenant_db.refresh(endpoint)

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type="job.created",
        payload={"job_id": "123"},
        idempotency_key=str(uuid4()),
        attempt_count=len(RETRY_DELAYS) - 1,  # one more failure will exceed the limit
        status="pending",
        company_id="tenant-test",
    )
    tenant_db.add(delivery)
    tenant_db.commit()
    tenant_db.refresh(delivery)

    asyncio.run(deliver_webhook(str(delivery.id), tenant_db))

    tenant_db.refresh(delivery)
    assert delivery.status == "abandoned"

    dlq_entry = tenant_db.query(AIAction).filter_by(action_type="webhook_dlq").one_or_none()
    assert dlq_entry is not None
    assert dlq_entry.priority == "high"
    assert dlq_entry.status == "pending"
    assert dlq_entry.payload["event_type"] == "job.created"
    assert dlq_entry.payload["endpoint_id"] == str(endpoint.id)
    assert dlq_entry.payload["attempt_count"] == len(RETRY_DELAYS)


def test_webhook_delivery_stats(tenant_db):
    """Stats endpoint returns correct delivery_rate: delivered/total."""
    from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint
    from gdx_dispatch.core.webhooks.monitor import get_webhook_stats

    endpoint = WebhookEndpoint(url="https://example.com/stats-hook", secret="secret", events=["job.created"])
    tenant_db.add(endpoint)
    tenant_db.commit()
    tenant_db.refresh(endpoint)

    deliveries = [
        WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type="job.created",
            payload={},
            idempotency_key=str(uuid4()),
            status="delivered",
            company_id="tenant-test",
        ),
        WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type="job.created",
            payload={},
            idempotency_key=str(uuid4()),
            status="abandoned",
            company_id="tenant-test",
        ),
        WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type="job.created",
            payload={},
            idempotency_key=str(uuid4()),
            status="pending",
            company_id="tenant-test",
        ),
    ]
    tenant_db.add_all(deliveries)
    tenant_db.commit()

    result = get_webhook_stats(db=tenant_db)

    matching = [s for s in result["stats"] if s["endpoint_id"] == endpoint.id]
    assert len(matching) == 1
    stat = matching[0]
    assert stat["total"] == 3
    assert stat["delivered"] == 1
    assert stat["abandoned"] == 1
    assert stat["pending"] == 1
    assert abs(stat["delivery_rate"] - 1 / 3) < 1e-9
