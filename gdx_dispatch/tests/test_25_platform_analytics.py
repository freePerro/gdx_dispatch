"""gdx_dispatch/tests/test_25_platform_analytics.py — Platform analytics and tenant KPI tests.

Tests use in-memory SQLite DBs (control + tenant) and mock Redis so no external
services are required.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant, TenantModuleGrant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, Job

# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

def _make_control_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _make_tenant_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


@pytest.fixture
def control_db():
    engine, Session = _make_control_db()
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def tenant_db():
    engine, Session = _make_tenant_db()
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def tenant_record(control_db):
    """Create a test tenant row in the control DB."""
    t = Tenant(
        id=uuid.uuid4(),
        slug="test-tenant",
        name="Test Tenant Co",  # plaintext in dev mode
        created_at=datetime.now(timezone.utc),
    )
    control_db.add(t)
    control_db.commit()
    control_db.refresh(t)
    return t


@pytest.fixture
def seeded_tenant_db(tenant_db):
    """Tenant DB pre-seeded with jobs and invoices."""
    now = datetime.now(timezone.utc)

    # Create 5 jobs (3 completed, 1 in_progress, 1 lead)
    jobs = []
    for i in range(5):
        stage = "completed" if i < 3 else ("in_progress" if i == 3 else "lead")
        j = Job(
            id=uuid.uuid4(),
            title=f"Job {i}",
            lifecycle_stage=stage,
            dispatch_status="done" if stage == "completed" else "unassigned",
            billing_status="paid" if stage == "completed" else "unbilled",
            assigned_to="tech_alice" if i % 2 == 0 else "tech_bob",
            completed_at=now - timedelta(days=i) if stage == "completed" else None,
            created_at=now - timedelta(days=i + 1),
            company_id="tenant-test",
        )
        tenant_db.add(j)
        jobs.append(j)

    tenant_db.flush()

    # Create invoices for completed jobs
    for idx, j in enumerate(jobs[:3]):
        inv = Invoice(
            customer_id=uuid.uuid4(),
            id=uuid.uuid4(),
            job_id=j.id,
            invoice_number=f"INV-TEST-{idx:03d}",
            billing_type="standard",
            subtotal=100.0,
            tax_amount=10.0,
            total=110.0,
            status="paid",
            public_token=f"tok-test-{idx}",
            paid_at=j.completed_at,
            created_at=j.completed_at or now,
            company_id="tenant-test",
        )
        tenant_db.add(inv)

    tenant_db.commit()
    return tenant_db


# ---------------------------------------------------------------------------
# Test 1: PlatformAnalytics.get_tenant_kpis — basic shape
# ---------------------------------------------------------------------------

def test_get_tenant_kpis_returns_expected_keys(seeded_tenant_db):
    from gdx_dispatch.core.platform_analytics import PlatformAnalytics

    pa = PlatformAnalytics()
    result = pa.get_tenant_kpis("test-tenant-id", "30d", seeded_tenant_db)

    assert isinstance(result, dict)
    for key in ("total_jobs", "revenue", "avg_job_value", "close_rate",
                "customer_satisfaction", "technician_utilization"):
        assert key in result, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Test 2: get_tenant_kpis — correct totals
# ---------------------------------------------------------------------------

def test_get_tenant_kpis_correct_totals(seeded_tenant_db):
    from gdx_dispatch.core.platform_analytics import PlatformAnalytics

    pa = PlatformAnalytics()
    result = pa.get_tenant_kpis("test-id", "90d", seeded_tenant_db)

    assert result["total_jobs"] == 5
    assert result["revenue"] == pytest.approx(330.0, abs=0.01)  # 3 * 110.0
    assert result["close_rate"] == pytest.approx(60.0, abs=0.1)  # 3/5 * 100


# ---------------------------------------------------------------------------
# Test 3: get_tenant_kpis — empty DB returns safe defaults
# ---------------------------------------------------------------------------

def test_get_tenant_kpis_empty_db(tenant_db):
    from gdx_dispatch.core.platform_analytics import PlatformAnalytics

    pa = PlatformAnalytics()
    result = pa.get_tenant_kpis("empty-tenant", "30d", tenant_db)

    assert result["total_jobs"] == 0
    assert result["revenue"] == 0.0
    assert result["close_rate"] == 0.0
    assert result["technician_utilization"] == 0.0
    assert result["customer_satisfaction"] == 0.0


# ---------------------------------------------------------------------------
# Test 4: get_platform_metrics_summary — returns expected keys
# ---------------------------------------------------------------------------

def test_get_platform_metrics_summary_keys(control_db, tenant_record):
    from gdx_dispatch.core.platform_analytics import PlatformAnalytics

    pa = PlatformAnalytics()

    # Mock get_platform_metrics to avoid cross-tenant DB opens
    mock_pm = MagicMock()
    mock_pm.total_tenants = 5
    mock_pm.active_tenants = 3
    mock_pm.churn_risk_count = 1
    mock_pm.total_jobs = 30
    mock_pm.avg_revenue_per_tenant = 1500.0
    mock_pm.new_tenants_this_period = 2

    with patch("gdx_dispatch.core.platform_analytics.get_platform_metrics", return_value=mock_pm):
        result = pa.get_platform_metrics_summary(control_db)

    assert result["total_tenants"] == 5
    assert result["active_tenants"] == 3
    assert result["mrr"] == 1500.0
    assert result["churn_rate"] == pytest.approx(20.0, abs=0.1)  # 1/5 * 100
    assert result["avg_jobs_per_tenant"] == pytest.approx(10.0, abs=0.1)  # 30/3


# ---------------------------------------------------------------------------
# Test 5: get_cohort_analysis — valid month
# ---------------------------------------------------------------------------

def test_get_cohort_analysis_valid_month(control_db, tenant_record):
    from gdx_dispatch.core.platform_analytics import PlatformAnalytics

    pa = PlatformAnalytics()
    # tenant_record was created "now", so cohort for current month should have >= 1
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    result = pa.get_cohort_analysis(current_month, control_db)

    assert isinstance(result, list)
    assert len(result) == 1
    row = result[0]
    assert row["cohort_month"] == current_month
    assert row["cohort_size"] >= 1
    assert "active_m1" in row
    assert "active_m2" in row
    assert "active_m3" in row


# ---------------------------------------------------------------------------
# Test 6: get_feature_adoption — granted modules returned
# ---------------------------------------------------------------------------

def test_get_feature_adoption_returns_grants(control_db, tenant_record):
    from gdx_dispatch.core.platform_analytics import PlatformAnalytics

    # Add module grants
    for module in ("quickbooks", "timeclock", "proposals"):
        control_db.add(
            TenantModuleGrant(
                id=uuid.uuid4(),
                tenant_id=tenant_record.id,
                module_key=module,
                granted_at=datetime.now(timezone.utc),
            )
        )
    control_db.commit()

    pa = PlatformAnalytics()
    result = pa.get_feature_adoption(str(tenant_record.id), control_db)

    assert isinstance(result, dict)
    assert set(result["modules_granted"]) == {"quickbooks", "timeclock", "proposals"}
    assert "usage_frequency" in result
    assert isinstance(result["modules_used"], list)


# ---------------------------------------------------------------------------
# Test 7: /api/platform/kpis endpoint — 404 for unknown tenant
# ---------------------------------------------------------------------------

def test_kpis_endpoint_404_unknown_tenant(control_db):
    import gdx_dispatch.core.platform_analytics as _mod
    from gdx_dispatch.core.database import get_db

    app = FastAPI()
    app.include_router(_mod.router)
    app.dependency_overrides[get_db] = lambda: control_db
    # Override the actual callable inside the Depends object
    app.dependency_overrides[_mod._admin_dep.dependency] = lambda: None

    with TestClient(app, raise_server_exceptions=False) as client:
        unknown_id = str(uuid.uuid4())
        resp = client.get(f"/api/platform/kpis?tenant_id={unknown_id}&period=30d")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 8: /api/platform/revenue-trend endpoint — returns list
# ---------------------------------------------------------------------------

def test_revenue_trend_endpoint_returns_list(control_db, tenant_record, seeded_tenant_db):
    import gdx_dispatch.core.platform_analytics as _mod
    from gdx_dispatch.core.database import get_db

    app = FastAPI()
    app.include_router(_mod.router)
    app.dependency_overrides[get_db] = lambda: control_db
    app.dependency_overrides[_mod._admin_dep.dependency] = lambda: None

    # Patch _open_tenant_session to return seeded_tenant_db
    with patch(
        "gdx_dispatch.core.platform_analytics._open_tenant_session",
        return_value=seeded_tenant_db,
    ), patch("gdx_dispatch.core.platform_analytics._redis") as mock_redis_fn:
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.setex.return_value = None
        mock_redis_fn.return_value = mock_redis

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                f"/api/platform/revenue-trend?tenant_id={tenant_record.id}&months=12"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
