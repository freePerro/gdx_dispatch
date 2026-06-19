"""Tests for gdx_dispatch/core/sla_monitor.py and gdx_dispatch/core/status_page.py."""
from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_mock(store: dict | None = None) -> MagicMock:
    """Return a MagicMock that behaves like a minimal Redis client."""
    store = store if store is not None else {}
    r = MagicMock()

    # Sorted set
    r.pipeline.return_value.__enter__ = MagicMock(return_value=r.pipeline.return_value)
    r.pipeline.return_value.__exit__ = MagicMock(return_value=False)
    r.pipeline.return_value.execute = MagicMock(return_value=[])
    r.pipeline.return_value.zadd = MagicMock()
    r.pipeline.return_value.zremrangebyrank = MagicMock()
    r.pipeline.return_value.lpush = MagicMock()
    r.pipeline.return_value.ltrim = MagicMock()

    r.zrangebyscore = MagicMock(return_value=store.get("zrangebyscore", []))
    r.lrange = MagicMock(return_value=store.get("lrange", []))
    r.get = MagicMock(return_value=store.get("get", None))
    r.set = MagicMock()
    return r


# ---------------------------------------------------------------------------
# test_latency_tracking
# ---------------------------------------------------------------------------

def test_latency_tracking():
    """track_request_latency should pipeline zadd + lpush without raising."""
    from gdx_dispatch.core import sla_monitor

    mock_redis = _make_redis_mock()

    with patch.object(sla_monitor, "_get_redis", return_value=mock_redis):
        # Should not raise
        sla_monitor.track_request_latency("/api/jobs", 120.5, 200)
        sla_monitor.track_request_latency("/api/jobs", 600.0, 500)

    # Pipeline was called twice (once per track call)
    assert mock_redis.pipeline.call_count == 2
    pipe = mock_redis.pipeline.return_value
    assert pipe.zadd.call_count == 2
    assert pipe.lpush.call_count == 2
    assert pipe.execute.call_count == 2


# ---------------------------------------------------------------------------
# test_sla_violation_detection
# ---------------------------------------------------------------------------

def test_sla_violation_detection():
    """check_sla_violations detects p99 > 500ms and error_rate > 0.1%."""
    from gdx_dispatch.core import sla_monitor

    now = time.time()
    # 100 entries: 99 slow (600ms, 200) + 1 error (100ms, 500)
    entries = [f"600.0:200:{now - i}" for i in range(99)]
    entries.append(f"100.0:500:{now - 100}")

    mock_redis = _make_redis_mock({"zrangebyscore": entries, "lrange": []})

    with patch.object(sla_monitor, "_get_redis", return_value=mock_redis):
        violations = sla_monitor.check_sla_violations("/api/jobs")

    p99_v = next(v for v in violations if v["metric"] == "p99_latency")
    err_v = next(v for v in violations if v["metric"] == "error_rate")
    up_v  = next(v for v in violations if v["metric"] == "uptime")

    assert p99_v["violated"] is True, "p99 of 600ms should violate 500ms target"
    assert p99_v["value"] == 600.0

    assert err_v["violated"] is True, "1% error rate should violate 0.1% target"
    assert err_v["value"] == pytest.approx(1.0, abs=0.01)

    # No health-check data → uptime defaults to 100.0 → no violation
    assert up_v["violated"] is False


# ---------------------------------------------------------------------------
# test_status_page_public_accessible
# ---------------------------------------------------------------------------

def test_status_page_public_accessible():
    """get_current_status returns a valid structure with all SERVICE_COMPONENTS."""
    from gdx_dispatch.core import status_page

    mock_redis = _make_redis_mock({"get": None})  # no stored statuses

    with patch.object(status_page, "_get_redis", return_value=mock_redis):
        result = status_page.get_current_status()

    assert "overall" in result
    assert result["overall"] == "operational"
    assert "services" in result
    names = [s["name"] for s in result["services"]]
    for component in status_page.SERVICE_COMPONENTS:
        assert component in names, f"{component} missing from status response"
    assert "updated_at" in result


# ---------------------------------------------------------------------------
# test_incident_lifecycle
# ---------------------------------------------------------------------------

def test_incident_lifecycle():
    """record_incident → resolve_incident → get_incident_history roundtrip."""
    from gdx_dispatch.core import status_page

    incident_store: dict[str, str | None] = {"get": None}

    def fake_get(key: str) -> str | None:
        return incident_store.get("get")

    def fake_set(key: str, value: str) -> None:
        incident_store["get"] = value

    mock_redis = _make_redis_mock()
    mock_redis.get.side_effect = fake_get
    mock_redis.set.side_effect = fake_set

    with patch.object(status_page, "_get_redis", return_value=mock_redis):
        inc_id = status_page.record_incident(
            title="API latency spike",
            description="p99 exceeded 500ms for 10 minutes",
            severity="major",
        )
        assert isinstance(inc_id, str) and len(inc_id) == 32

        resolved = status_page.resolve_incident(inc_id, resolution="Scaled up API pods")
        assert resolved is True

        history = status_page.get_incident_history(days=30)

    assert len(history) == 1
    inc = history[0]
    assert inc["id"] == inc_id
    assert inc["status"] == "resolved"
    assert inc["resolution"] == "Scaled up API pods"
    assert inc["resolved_at"] is not None


# ---------------------------------------------------------------------------
# test_uptime_calculation
# ---------------------------------------------------------------------------

def test_uptime_calculation():
    """get_uptime_percentage returns correct % from health_checks list."""
    from gdx_dispatch.core import sla_monitor

    now = time.time()
    # 90 up (200) + 10 down (500) within the last 30 days
    entries = [
        json.dumps({"ts": now - i * 100, "status_code": 200})
        for i in range(90)
    ] + [
        json.dumps({"ts": now - i * 100, "status_code": 500})
        for i in range(90, 100)
    ]

    mock_redis = _make_redis_mock({"lrange": entries})

    with patch.object(sla_monitor, "_get_redis", return_value=mock_redis):
        pct = sla_monitor.get_uptime_percentage(window_days=30)

    assert pct == pytest.approx(90.0, abs=0.01), f"Expected 90.0%, got {pct}"
