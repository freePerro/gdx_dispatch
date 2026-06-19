"""Tests for gdx_dispatch/core/recommendations.py and gdx_dispatch/core/next_action.py."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gdx_dispatch.core.next_action import NextActionQueue  # noqa: E402
from gdx_dispatch.core.recommendations import RecommendationEngine  # noqa: E402

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _setup_customer_and_job(tenant_db, lifecycle_stage="completed", billing_status="unbilled"):
    from gdx_dispatch.models.tenant_models import Customer, Job

    now = datetime.now(timezone.utc)
    cust = Customer(name="Test Customer", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(
        title="Test Job",
        customer_id=cust.id,
        lifecycle_stage=lifecycle_stage,
        billing_status=billing_status,
        created_at=now - timedelta(days=5),
        company_id="tenant-test",
    )
    tenant_db.add(job)
    tenant_db.flush()
    return cust, job


# ===========================================================================
# RecommendationEngine tests
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 1: get_job_recommendations — invoice_now fires for completed+unbilled
# ---------------------------------------------------------------------------

def test_job_recommendation_invoice_now(tenant_db, control_db):
    """invoice_now recommendation fires when job is completed and unbilled."""
    rec_engine = RecommendationEngine()
    tenant_id = str(uuid.uuid4())

    cust, job = _setup_customer_and_job(
        tenant_db, lifecycle_stage="completed", billing_status="unbilled"
    )
    tenant_db.commit()

    recs = rec_engine.get_job_recommendations(tenant_id, str(job.id), tenant_db)
    types = [r["type"] for r in recs]
    assert "invoice_now" in types
    rec = next(r for r in recs if r["type"] == "invoice_now")
    assert rec["priority"] == "high"
    assert f"/jobs/{job.id}/invoice/new" in rec["action_url"]


# ---------------------------------------------------------------------------
# Test 2: get_job_recommendations — send_estimate fires for stale estimate
# ---------------------------------------------------------------------------

def test_job_recommendation_send_estimate(tenant_db, control_db):
    """send_estimate fires when estimate is >48h old with no invoice."""
    from gdx_dispatch.models.tenant_models import Customer, Job

    rec_engine = RecommendationEngine()
    tenant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    cust = Customer(name="Estimate Customer", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(
        title="Stale Estimate",
        customer_id=cust.id,
        lifecycle_stage="estimate",
        billing_status="unbilled",
        created_at=now - timedelta(hours=72),
        company_id="tenant-test",
    )
    tenant_db.add(job)
    tenant_db.commit()

    recs = rec_engine.get_job_recommendations(tenant_id, str(job.id), tenant_db)
    types = [r["type"] for r in recs]
    assert "send_estimate" in types
    rec = next(r for r in recs if r["type"] == "send_estimate")
    assert rec["priority"] == "high"


# ---------------------------------------------------------------------------
# Test 3: get_customer_recommendations — upsell fires for high-value customer
# ---------------------------------------------------------------------------

def test_customer_recommendation_upsell_maintenance(tenant_db, control_db):
    """upsell_maintenance_plan fires for high avg invoice customer with no maintenance job."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

    rec_engine = RecommendationEngine()
    tenant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    cust = Customer(name="Premium Customer", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(
        title="Spring Repair",
        customer_id=cust.id,
        lifecycle_stage="completed",
        created_at=now - timedelta(days=30),
        company_id="tenant-test",
    )
    tenant_db.add(job)
    tenant_db.flush()

    tenant_db.add(Invoice(
        customer_id=uuid.uuid4(),
        job_id=job.id,
        invoice_number="INV-HV-001",
        status="paid",
        paid_at=now - timedelta(days=5),
        sent_at=now - timedelta(days=6),
        total=800.0,
        public_token="tok-hv-001",
        created_at=now - timedelta(days=7),
        company_id="tenant-test",
    ))
    tenant_db.commit()

    recs = rec_engine.get_customer_recommendations(
        tenant_id, str(cust.id), tenant_db
    )
    types = [r["type"] for r in recs]
    assert "upsell_maintenance_plan" in types
    rec = next(r for r in recs if r["type"] == "upsell_maintenance_plan")
    assert rec["priority"] == "high"
    assert rec["estimated_value"] > 0.0


# ---------------------------------------------------------------------------
# Test 4: get_customer_recommendations — request_review for first-time customer
# ---------------------------------------------------------------------------

def test_customer_recommendation_request_review(tenant_db, control_db):
    """request_review fires when customer has exactly 1 completed job."""
    rec_engine = RecommendationEngine()
    tenant_id = str(uuid.uuid4())

    cust, job = _setup_customer_and_job(
        tenant_db, lifecycle_stage="completed", billing_status="paid"
    )
    tenant_db.commit()

    recs = rec_engine.get_customer_recommendations(
        tenant_id, str(cust.id), tenant_db
    )
    types = [r["type"] for r in recs]
    assert "request_review" in types
    rec = next(r for r in recs if r["type"] == "request_review")
    assert rec["priority"] == "low"


# ---------------------------------------------------------------------------
# Test 5: get_operational_recommendations — unassigned jobs alert
# ---------------------------------------------------------------------------

def test_operational_recommendation_unassigned_jobs(tenant_db, control_db):
    """unassigned_jobs_alert fires when >3 scheduled jobs have no technician."""
    from gdx_dispatch.models.tenant_models import Customer, Job

    rec_engine = RecommendationEngine()
    tenant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    cust = Customer(name="Ops Customer", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    for i in range(5):
        tenant_db.add(Job(
            title=f"Unassigned Job {i}",
            customer_id=cust.id,
            lifecycle_stage="scheduled",
            dispatch_status="unassigned",
            created_at=now - timedelta(days=i),
            company_id="tenant-test",
        ))
    tenant_db.commit()

    recs = rec_engine.get_operational_recommendations(tenant_id, tenant_db)
    types = [r["type"] for r in recs]
    assert "unassigned_jobs_alert" in types
    rec = next(r for r in recs if r["type"] == "unassigned_jobs_alert")
    assert rec["priority"] == "high"


# ---------------------------------------------------------------------------
# Test 6: get_revenue_recommendations — unbilled_work_alert fires
# ---------------------------------------------------------------------------

def test_revenue_recommendation_unbilled_work(tenant_db, control_db):
    """unbilled_work_alert fires when >10 completed jobs are unbilled in 30d."""
    from gdx_dispatch.models.tenant_models import Customer, Job

    rec_engine = RecommendationEngine()
    tenant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    cust = Customer(name="Revenue Customer", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    for i in range(12):
        tenant_db.add(Job(
            title=f"Completed Job {i}",
            customer_id=cust.id,
            lifecycle_stage="completed",
            billing_status="unbilled",
            completed_at=now - timedelta(days=i + 1),
            created_at=now - timedelta(days=i + 2),
            company_id="tenant-test",
        ))
    tenant_db.commit()

    recs = rec_engine.get_revenue_recommendations(tenant_id, tenant_db)
    types = [r["type"] for r in recs]
    assert "unbilled_work_alert" in types
    rec = next(r for r in recs if r["type"] == "unbilled_work_alert")
    assert rec["priority"] == "high"


# ===========================================================================
# NextActionQueue tests
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 7: NextActionQueue — create and retrieve persisted action
# ---------------------------------------------------------------------------

def test_next_action_queue_create_and_retrieve(tenant_db, control_db):
    """Created actions appear in the queue."""
    q = NextActionQueue()
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    result = q.create_action(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="follow_up_estimate",
        title="Follow Up on Estimate #123",
        description="Call the customer back.",
        priority="high",
        action_url="/jobs/123",
        estimated_value=250.0,
        reference_id="job-123",
        tenant_db=tenant_db,
    )

    assert "error" not in result
    assert result["action_type"] == "follow_up_estimate"
    assert result["priority"] == "high"
    assert result["status"] == "pending"

    # Queue should include this action
    queue_items = q.get_queue(tenant_id, user_id, tenant_db)
    ids = [item["id"] for item in queue_items]
    assert result["id"] in ids


# ---------------------------------------------------------------------------
# Test 8: NextActionQueue — complete and snooze actions
# ---------------------------------------------------------------------------

def test_next_action_queue_complete_and_snooze(tenant_db, control_db):
    """complete_action and snooze_action update status correctly."""
    q = NextActionQueue()
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    # Create two actions
    action_a = q.create_action(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="call_overdue_invoice",
        title="Call Customer A",
        description=None,
        priority="high",
        action_url="/invoices/1",
        estimated_value=500.0,
        reference_id="inv-1",
        tenant_db=tenant_db,
    )
    action_b = q.create_action(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="request_review",
        title="Request Review",
        description=None,
        priority="low",
        action_url="/customers/1",
        estimated_value=0.0,
        reference_id="cust-1",
        tenant_db=tenant_db,
    )

    # Complete action A
    done = q.complete_action(tenant_id, action_a["id"], tenant_db)
    assert done["status"] == "completed"

    # Snooze action B until tomorrow
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    snoozed = q.snooze_action(tenant_id, action_b["id"], tomorrow, tenant_db)
    assert snoozed["status"] == "snoozed"
    assert "snoozed_until" in snoozed

    # Queue should now exclude both
    queue_items = q.get_queue(tenant_id, user_id, tenant_db)
    persisted_ids = [
        item["id"]
        for item in queue_items
        if not str(item["id"]).startswith("auto:")
    ]
    assert action_a["id"] not in persisted_ids
    assert action_b["id"] not in persisted_ids
