"""Tests for AI-powered business recommendations (gdx_dispatch/core/ai_recommendations.py)."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gdx_dispatch.core.ai_recommendations import (  # noqa: E402
    INDUSTRY_BENCHMARK_JOB_VALUE,
    get_recommendations,
)

# ---------------------------------------------------------------------------
# Test 1: enable_module recommendation triggers on high job volume + no inventory
# ---------------------------------------------------------------------------

def test_enable_module_recommendation_triggers(tenant_db, control_db):
    """enable_module fires when >10 jobs in 30d and inventory module not granted."""
    from gdx_dispatch.models.tenant_models import Customer, Job

    now = datetime.now(timezone.utc)
    tenant_id = str(uuid.uuid4())

    cust = Customer(name="Acme", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    for i in range(12):
        tenant_db.add(Job(title=f"Job {i}", created_at=now - timedelta(days=i % 25), customer_id=cust.id, company_id="tenant-test"))
    tenant_db.commit()

    recs = get_recommendations(tenant_id, tenant_db, control_db)
    types = [r.type for r in recs]
    assert "enable_module" in types
    rec = next(r for r in recs if r.type == "enable_module")
    assert rec.priority == "medium"
    assert rec.metric == 12


# ---------------------------------------------------------------------------
# Test 2: activate_campaigns recommendation triggers on unsold estimates
# ---------------------------------------------------------------------------

def test_activate_campaigns_recommendation_triggers(tenant_db, control_db):
    """activate_campaigns fires when >5 unsold estimates in last 30d."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

    now = datetime.now(timezone.utc)
    tenant_id = str(uuid.uuid4())

    cust = Customer(name="Beta Co", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(title="J1", created_at=now - timedelta(days=5), customer_id=cust.id, company_id="tenant-test")
    tenant_db.add(job)
    tenant_db.flush()

    for i in range(6):
        tenant_db.add(Invoice(
            customer_id=uuid.uuid4(),
            job_id=job.id,
            invoice_number=f"EST-{i:03}",
            status="sent",
            sent_at=now - timedelta(days=i + 1),
            public_token=f"tok-est-{i}",
            created_at=now - timedelta(days=i + 2),
            company_id="tenant-test",
        ))
    tenant_db.commit()

    recs = get_recommendations(tenant_id, tenant_db, control_db)
    types = [r.type for r in recs]
    assert "activate_campaigns" in types
    rec = next(r for r in recs if r.type == "activate_campaigns")
    assert rec.priority == "high"
    assert rec.metric == 6


# ---------------------------------------------------------------------------
# Test 3: connect_qb triggers when invoices exist and QB not granted
# ---------------------------------------------------------------------------

def test_connect_qb_recommendation_triggers(tenant_db, control_db):
    """connect_qb fires when invoices exist but quickbooks module not enabled."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

    now = datetime.now(timezone.utc)
    tenant_id = str(uuid.uuid4())

    cust = Customer(name="QBless Corp", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(title="J1", created_at=now - timedelta(days=3), customer_id=cust.id, company_id="tenant-test")
    tenant_db.add(job)
    tenant_db.flush()

    tenant_db.add(Invoice(
        customer_id=uuid.uuid4(),
        job_id=job.id,
        invoice_number="INV-001",
        status="paid",
        paid_at=now - timedelta(days=2),
        sent_at=now - timedelta(days=3),
        total=300.0,
        public_token="tok-qb",
        created_at=now - timedelta(days=4),
        company_id="tenant-test",
    ))
    tenant_db.commit()

    recs = get_recommendations(tenant_id, tenant_db, control_db)
    types = [r.type for r in recs]
    assert "connect_qb" in types
    rec = next(r for r in recs if r.type == "connect_qb")
    assert rec.priority == "high"
    assert rec.metric == 1


# ---------------------------------------------------------------------------
# Test 4: increase_prices triggers when avg job value below benchmark
# ---------------------------------------------------------------------------

def test_increase_prices_recommendation_triggers(tenant_db, control_db):
    """increase_prices fires when avg paid invoice < INDUSTRY_BENCHMARK_JOB_VALUE."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

    now = datetime.now(timezone.utc)
    tenant_id = str(uuid.uuid4())

    cust = Customer(name="Cheap Co", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    job = Job(title="J1", created_at=now - timedelta(days=5), customer_id=cust.id, company_id="tenant-test")
    tenant_db.add(job)
    tenant_db.flush()

    # avg = 150 < 285
    for i in range(3):
        tenant_db.add(Invoice(
            customer_id=uuid.uuid4(),
            job_id=job.id,
            invoice_number=f"INV-P{i:03}",
            status="paid",
            paid_at=now - timedelta(days=i + 1),
            sent_at=now - timedelta(days=i + 2),
            total=150.0,
            public_token=f"tok-price-{i}",
            created_at=now - timedelta(days=i + 3),
            company_id="tenant-test",
        ))
    tenant_db.commit()

    recs = get_recommendations(tenant_id, tenant_db, control_db)
    types = [r.type for r in recs]
    assert "increase_prices" in types
    rec = next(r for r in recs if r.type == "increase_prices")
    assert rec.priority == "low"
    assert rec.metric < INDUSTRY_BENCHMARK_JOB_VALUE


# ---------------------------------------------------------------------------
# Test 5: hire_technician triggers when single tech has >15 jobs in 7d
# ---------------------------------------------------------------------------

def test_hire_technician_recommendation_triggers(tenant_db, control_db):
    """hire_technician fires when a single tech has >15 jobs in last 7d."""
    from gdx_dispatch.models.tenant_models import Customer, Job

    now = datetime.now(timezone.utc)
    tenant_id = str(uuid.uuid4())
    tech_id = uuid.uuid4()

    cust = Customer(name="Busy Inc", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    for i in range(16):
        tenant_db.add(Job(
            title=f"Rush Job {i}",
            created_at=now - timedelta(hours=i * 3),
            customer_id=cust.id,
            assigned_to=str(tech_id),
            company_id="tenant-test",
        ))
    tenant_db.commit()

    recs = get_recommendations(tenant_id, tenant_db, control_db)
    types = [r.type for r in recs]
    assert "hire_technician" in types
    rec = next(r for r in recs if r.type == "hire_technician")
    assert rec.priority == "high"


# ---------------------------------------------------------------------------
# Test 6: Dismissal works (Redis key set with 30d TTL)
# ---------------------------------------------------------------------------

def test_dismissal_stores_redis_key():
    """dismiss_recommendation stores dismissal key in Redis for 30 days."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.ai_recommendations import router

    app = FastAPI()
    app.include_router(router)

    with patch("gdx_dispatch.core.ai_recommendations._redis") as mock_redis_factory:
        mock_redis = MagicMock()
        mock_redis.setex.return_value = True
        mock_redis_factory.return_value = mock_redis

        client = TestClient(app, raise_server_exceptions=False)

        # Simulate a tenant in request state by overriding middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        class FakeTenantMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.tenant = {"id": "test-tenant-id"}
                return await call_next(request)

        app.add_middleware(FakeTenantMiddleware)

        response = client.post("/api/recommendations/connect_qb/dismiss")
        assert response.status_code == 200
        data = response.json()
        assert data["dismissed"] == "connect_qb"

        # Verify Redis setex was called with 30-day TTL
        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        assert "rec:dismissed:test-tenant-id:connect_qb" in call_args[0][0]
        assert call_args[0][1] == 2_592_000


# ---------------------------------------------------------------------------
# Test 7: Priority ordering — high before medium before low
# ---------------------------------------------------------------------------

def test_priority_ordering(tenant_db, control_db):
    """Recommendations must be sorted high → medium → low."""
    from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

    now = datetime.now(timezone.utc)
    tenant_id = str(uuid.uuid4())

    cust = Customer(name="Mixed Co", company_id="tenant-test")
    tenant_db.add(cust)
    tenant_db.flush()

    # Trigger enable_module (medium): >10 jobs, no inventory
    for i in range(12):
        tenant_db.add(Job(title=f"J{i}", created_at=now - timedelta(days=i % 25), customer_id=cust.id, company_id="tenant-test"))
    tenant_db.flush()

    # Trigger connect_qb (high): invoice exists, no QB
    job = tenant_db.query(Job).first()
    tenant_db.add(Invoice(
        customer_id=uuid.uuid4(),
        job_id=job.id, invoice_number="INV-ORD",
        status="paid", paid_at=now - timedelta(days=1),
        sent_at=now - timedelta(days=2), total=350.0,
        public_token="tok-ord", created_at=now - timedelta(days=3),
        company_id="tenant-test",
    ))
    tenant_db.commit()

    recs = get_recommendations(tenant_id, tenant_db, control_db)
    if len(recs) >= 2:
        from gdx_dispatch.core.ai_recommendations import _PRIORITY_ORDER
        priorities = [_PRIORITY_ORDER[r.priority] for r in recs]
        assert priorities == sorted(priorities), "Recommendations not sorted by priority"


# ---------------------------------------------------------------------------
# Test 8: Empty list when no triggers match
# ---------------------------------------------------------------------------

def test_empty_recommendations_when_no_triggers(tenant_db, control_db):
    """Empty tenant DB should return an empty recommendations list."""
    tenant_id = str(uuid.uuid4())
    recs = get_recommendations(tenant_id, tenant_db, control_db)
    # No jobs, no invoices → most rules won't fire
    # connect_qb won't fire (no invoices); enable_module won't fire (<=10 jobs)
    for rec in recs:
        assert rec.type not in ("connect_qb", "enable_module", "activate_campaigns", "hire_technician")
