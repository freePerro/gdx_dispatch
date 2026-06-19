"""P3.11 — call-reports reconcile."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComStatsDaily
from gdx_dispatch.modules.phone_com.reconcile import reconcile_recent

_VID = 1000000


@pytest.fixture
def tenant_session():
    e = create_engine("sqlite:///:memory:")
    PhoneComStatsDaily.__table__.create(e)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    return sm()


@respx.mock
def test_reconcile_no_drift_returns_zero(tenant_session):
    today = date.today()
    yest = today - timedelta(days=1)
    sess = tenant_session
    sess.add(PhoneComStatsDaily(
        stat_date=yest, calls_in=10, calls_out=5,
        calls_missed=1, total_call_minutes=42,
    ))
    sess.commit()

    respx.get(f"{BASE_URL}/accounts/{_VID}/call-reports").mock(
        return_value=httpx.Response(200, json={
            "filters": {}, "sort": {}, "total": 1,
            "limit": 25, "offset": None,
            "items": [{
                "date": yest.isoformat(),
                "calls_in": 10, "calls_out": 5,
                "calls_missed": 1, "total_minutes": 42,
            }],
        })
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    result = reconcile_recent(sess, c, days=7, drift_threshold=2)
    assert result["ok"] is True
    assert result["drift_count"] == 0


@respx.mock
def test_reconcile_reports_drift(tenant_session):
    today = date.today()
    yest = today - timedelta(days=1)
    sess = tenant_session
    sess.add(PhoneComStatsDaily(
        stat_date=yest, calls_in=10, calls_out=5,
        calls_missed=1, total_call_minutes=42,
    ))
    sess.commit()

    respx.get(f"{BASE_URL}/accounts/{_VID}/call-reports").mock(
        return_value=httpx.Response(200, json={
            "filters": {}, "sort": {}, "total": 1,
            "limit": 25, "offset": None,
            "items": [{
                "date": yest.isoformat(),
                "calls_in": 18,  # +8 vs local — 5 missed webhooks
                "calls_out": 5,
                "calls_missed": 1, "total_minutes": 42,
            }],
        })
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    result = reconcile_recent(sess, c, days=7, drift_threshold=2)
    assert result["drift_count"] == 1
    assert result["drifts"][0]["diffs"] == {"calls_in": 8}


@respx.mock
def test_reconcile_missing_local_row_treated_as_zero(tenant_session):
    today = date.today()
    yest = today - timedelta(days=1)
    sess = tenant_session
    # No local row at all.

    respx.get(f"{BASE_URL}/accounts/{_VID}/call-reports").mock(
        return_value=httpx.Response(200, json={
            "filters": {}, "sort": {}, "total": 1,
            "limit": 25, "offset": None,
            "items": [{"date": yest.isoformat(), "calls_in": 7,
                       "calls_out": 0, "calls_missed": 0, "total_minutes": 0}],
        })
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    result = reconcile_recent(sess, c, days=7, drift_threshold=2)
    assert result["drift_count"] == 1
    assert result["drifts"][0]["diffs"] == {"calls_in": 7}


@respx.mock
def test_reconcile_handles_upstream_500(tenant_session):
    sess = tenant_session
    respx.get(f"{BASE_URL}/accounts/{_VID}/call-reports").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    c = PhoneComClient(token="t", voip_id=_VID)
    c.retry_max_attempts = 0
    result = reconcile_recent(sess, c, days=7)
    assert result["ok"] is False
