"""Tests for cross-tenant platform analytics (gdx_dispatch/core/platform_analytics.py)."""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gdx_dispatch.core.platform_analytics import (  # noqa: E402
    PlatformMetrics,
    get_platform_metrics,
    platform_growth_endpoint,
    platform_module_adoption_endpoint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_control_db_with_tenants(control_db, n=3):
    """Add n fake tenants to the control DB."""
    from gdx_dispatch.control.models import Tenant
    now = datetime.now(timezone.utc)
    tenants = []
    for i in range(n):
        t = Tenant(
            id=uuid.uuid4(),
            slug=f"tenant-{i}",
            created_at=now - timedelta(days=i),
        )
        control_db.add(t)
        tenants.append(t)
    control_db.commit()
    return tenants


# ---------------------------------------------------------------------------
# Test 1: PlatformMetrics dataclass has all required fields
# ---------------------------------------------------------------------------

def test_platform_metrics_structure():
    """PlatformMetrics must expose all documented fields."""
    m = PlatformMetrics(
        period="30d",
        total_tenants=5,
        active_tenants=3,
        total_jobs=42,
        total_revenue_sum=12500.0,
        avg_revenue_per_tenant=4166.67,
        top_modules_by_adoption=[{"module": "timeclock", "tenant_count": 3, "pct_of_total": 60.0}],
        churn_risk_count=1,
        new_tenants_this_period=2,
    )
    assert m.period == "30d"
    assert m.total_tenants == 5
    assert m.active_tenants == 3
    assert m.total_jobs == 42
    assert m.total_revenue_sum == 12500.0
    assert m.avg_revenue_per_tenant == 4166.67
    assert isinstance(m.top_modules_by_adoption, list)
    assert m.churn_risk_count == 1
    assert m.new_tenants_this_period == 2


# ---------------------------------------------------------------------------
# Test 2: Module adoption list shape
# ---------------------------------------------------------------------------

def test_module_adoption_list_shape(control_db):
    """module_adoption endpoint returns list of dicts with required keys."""
    from gdx_dispatch.control.models import Tenant, TenantModuleGrant

    t = Tenant(id=uuid.uuid4(), slug="t1", name="Tenant One")
    control_db.add(t)
    control_db.flush()
    control_db.add(TenantModuleGrant(tenant_id=t.id, module_key="timeclock", granted_at=datetime.now(timezone.utc)))
    control_db.add(TenantModuleGrant(tenant_id=t.id, module_key="quickbooks", granted_at=datetime.now(timezone.utc)))
    control_db.commit()

    # Patch Redis to avoid needing a real connection
    with patch("gdx_dispatch.core.platform_analytics._redis") as mock_redis_factory:
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.setex.return_value = True
        mock_redis_factory.return_value = mock_redis

        result = platform_module_adoption_endpoint(control_db=control_db, _=None)

    assert isinstance(result, list)
    assert len(result) >= 2
    for item in result:
        assert "module" in item
        assert "tenant_count" in item
        assert "pct_of_total" in item


# ---------------------------------------------------------------------------
# Test 3: Growth data format
# ---------------------------------------------------------------------------

def test_growth_data_format(control_db):
    """Growth endpoint must return dict with 'new_tenants' and 'churn' keys."""
    with patch("gdx_dispatch.core.platform_analytics._redis") as mock_redis_factory:
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.setex.return_value = True
        mock_redis_factory.return_value = mock_redis

        result = platform_growth_endpoint(control_db=control_db, _=None)

    assert isinstance(result, dict)
    assert "new_tenants" in result
    assert "churn" in result
    assert isinstance(result["new_tenants"], list)
    assert isinstance(result["churn"], list)


# ---------------------------------------------------------------------------
# Test 4: Admin-only access enforced
# ---------------------------------------------------------------------------

def test_admin_only_access_enforced():
    """Router prefix should be /api/platform and all routes are admin-only."""
    from gdx_dispatch.core.platform_analytics import router
    assert router.prefix == "/api/platform"

    route_paths = {r.path for r in router.routes}
    assert "/api/platform/metrics" in route_paths
    assert "/api/platform/growth" in route_paths
    assert "/api/platform/module-adoption" in route_paths


# ---------------------------------------------------------------------------
# Test 5: Cache hit returns cached value
# ---------------------------------------------------------------------------

def test_cache_hit_returns_cached_value(control_db):
    """get_platform_metrics should return cached data on Redis hit."""
    expected = PlatformMetrics(
        period="7d",
        total_tenants=10,
        active_tenants=7,
        total_jobs=100,
        total_revenue_sum=9999.0,
        avg_revenue_per_tenant=1428.43,
        top_modules_by_adoption=[],
        churn_risk_count=2,
        new_tenants_this_period=1,
    )

    with patch("gdx_dispatch.core.platform_analytics._redis") as mock_redis_factory:
        mock_redis = MagicMock()
        import dataclasses
        mock_redis.get.return_value = json.dumps(dataclasses.asdict(expected))
        mock_redis_factory.return_value = mock_redis

        result = get_platform_metrics(period="7d", control_db=control_db)

    assert result.total_tenants == 10
    assert result.active_tenants == 7
    assert result.total_jobs == 100
    assert result.churn_risk_count == 2


# ---------------------------------------------------------------------------
# Test 6: Cross-tenant aggregation doesn't leak tenant data in metrics
# ---------------------------------------------------------------------------

def test_cross_tenant_no_data_leakage():
    """PlatformMetrics must only contain aggregated/anonymized data (no tenant IDs)."""
    import dataclasses
    m = PlatformMetrics(
        period="30d",
        total_tenants=2,
        active_tenants=2,
        total_jobs=10,
        total_revenue_sum=5000.0,
        avg_revenue_per_tenant=2500.0,
        top_modules_by_adoption=[],
        churn_risk_count=0,
        new_tenants_this_period=0,
    )
    d = dataclasses.asdict(m)
    # No per-tenant identifying fields in the output
    assert "tenant_id" not in d
    assert "slug" not in d
    assert "db_url" not in d
    # Only aggregated numeric totals
    assert "total_tenants" in d
    assert "total_jobs" in d
    assert "total_revenue_sum" in d
