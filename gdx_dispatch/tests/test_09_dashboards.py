"""Tests for distributor and wholesaler dashboard API endpoints."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# Ensure the repo root is on sys.path so gdx is importable without install
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Route existence tests
# ---------------------------------------------------------------------------

def test_distributor_dashboard_route_exists():
    from gdx_dispatch.core.distributor_dashboard import distributor_router
    routes = [r.path for r in distributor_router.routes]
    assert len(routes) >= 1
    assert "/dashboard" in routes or "" in routes or any("dashboard" in r or r == "" for r in routes)


def test_wholesaler_dashboard_route_exists():
    from gdx_dispatch.core.wholesaler_dashboard import wholesaler_router
    routes = [r.path for r in wholesaler_router.routes]
    assert len(routes) >= 1
    assert "/dashboard" in routes or "" in routes or any("dashboard" in r or r == "" for r in routes)


# ---------------------------------------------------------------------------
# Response key tests — mock DB session so no real DB needed
# ---------------------------------------------------------------------------

def _make_mock_db_row(**kwargs):
    """Return a MagicMock that behaves like a SQLAlchemy Row with named attrs."""
    row = MagicMock()
    for k, v in kwargs.items():
        setattr(row, k, v)
    row._mapping = kwargs
    return row


def test_distributor_dashboard_returns_required_keys():
    from gdx_dispatch.core.distributor_dashboard import get_distributor_dashboard

    # Mock DB: return sensible rows for each query
    mock_db = MagicMock()
    summary_row = _make_mock_db_row(total_orders_30d=5, total_revenue_30d=1200.0, dealer_count=3)
    analytics_row = _make_mock_db_row(active_dealers=2)

    call_count = 0

    def execute_side_effect(query, params=None):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.fetchone.return_value = summary_row
            result.fetchall.return_value = []
        elif call_count == 2:
            result.fetchone.return_value = analytics_row
            result.fetchall.return_value = []
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result

    mock_db.execute.side_effect = execute_side_effect

    mock_request = MagicMock()
    mock_request.state.tenant = {"id": "tenant-abc"}

    response = get_distributor_dashboard(request=mock_request, db=mock_db)
    import json
    data = json.loads(response.body)

    assert "dealer_count" in data
    assert "active_dealer_count" in data
    assert "total_orders_30d" in data
    assert "total_revenue_30d" in data
    assert "top_dealers" in data
    assert isinstance(data["top_dealers"], list)


def test_wholesaler_dashboard_returns_required_keys():
    from gdx_dispatch.core.wholesaler_dashboard import get_wholesaler_dashboard

    mock_db = MagicMock()
    sku_row = _make_mock_db_row(sku_count=42, active_sku_count=38)
    channel_row = _make_mock_db_row(active_distributors=7, total_channel_revenue=9500.0)

    call_count = 0

    def execute_side_effect(query, params=None):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.fetchone.return_value = sku_row
        elif call_count == 2:
            result.fetchone.return_value = channel_row
        elif call_count == 3:
            orders_row = _make_mock_db_row(orders_received_30d=10, revenue_30d=3000.0)
            result.fetchone.return_value = orders_row
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = None
        return result

    mock_db.execute.side_effect = execute_side_effect

    mock_request = MagicMock()
    mock_request.state.tenant = {"id": "tenant-xyz"}

    response = get_wholesaler_dashboard(request=mock_request, db=mock_db)
    import json
    data = json.loads(response.body)

    assert "sku_count" in data
    assert "active_sku_count" in data
    assert "orders_received_30d" in data
    assert "revenue_30d" in data
    assert "top_channels" in data
    assert isinstance(data["top_channels"], list)
