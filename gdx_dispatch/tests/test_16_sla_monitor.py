"""Tests for gdx_dispatch/core/sla_monitor.py — SLA monitoring and uptime tracking."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Shared in-memory SQLite setup
# ---------------------------------------------------------------------------

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from gdx_dispatch.control.models import Base  # noqa: E402
from gdx_dispatch.core.sla_monitor import (  # noqa: E402
    API_SLA_PCT,
    DB_SLA_PCT,
    JOBS_SLA_PCT,
    RESPONSE_P95_MS,
    SLACheck,
    UptimeRecord,
    compute_uptime_pct,
    get_overall_status,
    router,
)


def _make_test_engine():
    """Create a fresh in-memory SQLite DB for each test invocation."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


# All tests use _fresh_db() for isolation — no shared engine


def _fresh_db():
    """Return a session backed by a fresh engine (no state from prior tests)."""
    engine = _make_test_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


# ---------------------------------------------------------------------------
# Helper: seed UptimeRecord rows
# ---------------------------------------------------------------------------


def _seed_records(db, check_name: str, ok: int, down: int, hours_back: int = 1) -> None:
    """Insert ok + down UptimeRecord rows all within the last hours_back hours."""
    now = datetime.now(timezone.utc)
    for i in range(ok):
        db.add(
            UptimeRecord(
                check_name=check_name,
                status="ok",
                response_ms=50.0,
                checked_at=now - timedelta(minutes=i + 1),
            )
        )
    for i in range(down):
        db.add(
            UptimeRecord(
                check_name=check_name,
                status="down",
                response_ms=None,
                checked_at=now - timedelta(minutes=ok + i + 1),
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# Test 1 — /api/status is public (no auth required) and returns correct shape
# ---------------------------------------------------------------------------


def test_status_endpoint_public_no_auth():
    """GET /api/status must be accessible without authentication."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.sla_monitor import router

    app = FastAPI()
    db = _fresh_db()

    def _override_db():
        try:
            yield db
        finally:
            pass

    from gdx_dispatch.core.database import get_db

    app.dependency_overrides[get_db] = _override_db
    # Override require_role so no auth is checked on public routes
    # (public routes don't use require_role — just confirm no 401 on /api/status)
    app.include_router(router)

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/status")
    db.close()

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 2 — /api/status returns valid structure
# ---------------------------------------------------------------------------


def test_status_response_structure():
    """Response from /api/status must include overall, components, uptime_30d, incidents."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    db = _fresh_db()

    # Seed one check
    check = SLACheck(
        check_type="api",
        check_name="api_health",
        last_status="ok",
        last_checked_at=datetime.now(timezone.utc),
        last_response_ms=42.0,
        uptime_24h_pct=100.0,
        uptime_7d_pct=100.0,
        incident_count_30d=0,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(check)
    db.commit()

    def _override_db():
        try:
            yield db
        finally:
            pass

    from gdx_dispatch.core.database import get_db

    app.dependency_overrides[get_db] = _override_db
    app.include_router(router)

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/status")
    db.close()

    assert resp.status_code == 200
    data = resp.json()
    assert "overall" in data
    assert "components" in data
    assert "uptime_30d" in data
    assert "incidents" in data
    assert data["overall"] in ("operational", "degraded", "outage")
    assert isinstance(data["components"], list)
    assert isinstance(data["incidents"], list)


# ---------------------------------------------------------------------------
# Test 3 — admin metrics endpoint requires authentication
# ---------------------------------------------------------------------------


def test_admin_metrics_auth_required():
    """GET /api/admin/sla/metrics must return 401/403 without valid admin role."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    app = FastAPI()
    db = _fresh_db()

    def _override_db():
        try:
            yield db
        finally:
            pass

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.modules import require_role

    def _reject_all(*args, **kwargs):
        raise HTTPException(status_code=403, detail="Forbidden")

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_role("admin", "owner")] = _reject_all
    app.include_router(router)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/admin/sla/metrics")
    db.close()

    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Test 4 — history endpoint returns filtered records
# ---------------------------------------------------------------------------


def test_admin_history_returns_data():
    """GET /api/admin/sla/history returns UptimeRecord rows for requested check."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import gdx_dispatch.core.sla_monitor as _mod

    app = FastAPI()
    db = _fresh_db()
    _seed_records(db, "api_health", ok=3, down=1)

    def _override_db():
        yield db

    def _no_auth():
        return None

    from gdx_dispatch.core.database import get_db

    # _admin_dep is a Depends(require_role(...)) object — use it directly as override key
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_mod._admin_dep.dependency] = _no_auth
    app.include_router(router)

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/admin/sla/history?check=api_health&days=7")
    db.close()

    assert resp.status_code == 200
    records = resp.json()
    assert isinstance(records, list)
    assert len(records) >= 1
    for r in records:
        assert r["check_name"] == "api_health"
        assert "status" in r
        assert "checked_at" in r


# ---------------------------------------------------------------------------
# Test 5 — compute_uptime_pct calculates correctly
# ---------------------------------------------------------------------------


def test_compute_uptime_pct():
    """compute_uptime_pct should return accurate % of ok records."""
    db = _fresh_db()
    _seed_records(db, "uptime_test", ok=8, down=2, hours_back=24)

    pct = compute_uptime_pct("uptime_test", 24, db)
    db.close()

    # 8 ok out of 10 total = 80.0%
    assert abs(pct - 80.0) < 0.1, f"Expected ~80.0, got {pct}"


def test_compute_uptime_pct_no_records():
    """compute_uptime_pct returns 100.0 when no records exist."""
    db = _fresh_db()
    pct = compute_uptime_pct("nonexistent_check", 24, db)
    db.close()
    assert pct == 100.0


# ---------------------------------------------------------------------------
# Test 6 — run_sla_checks_sync executes without crashing
# ---------------------------------------------------------------------------


def test_run_sla_checks_sync_runs():
    """run_sla_checks_sync must run all probes and return a list of result dicts."""
    from gdx_dispatch.core.sla_monitor import run_sla_checks_sync

    # Mock all external probes to avoid network/redis calls in test
    with (
        patch("gdx_dispatch.core.sla_monitor._check_api", return_value=("ok", 45.0)),
        patch("gdx_dispatch.core.sla_monitor._check_db", return_value=("ok", 5.0)),
        patch("gdx_dispatch.core.sla_monitor._check_redis", return_value=("ok", 3.0)),
        patch("gdx_dispatch.core.sla_monitor._check_celery", return_value=("ok", 4.0)),
        patch("gdx_dispatch.core.sla_monitor.SessionLocal", return_value=_fresh_db()),
    ):
        results = run_sla_checks_sync()

    assert isinstance(results, list)
    assert len(results) == 4
    for r in results:
        assert "check_name" in r
        assert "status" in r
        assert r["status"] in ("ok", "degraded", "down")


# ---------------------------------------------------------------------------
# Test 7 — get_overall_status logic
# ---------------------------------------------------------------------------


def test_get_overall_status_all_ok():
    checks = [MagicMock(last_status="ok"), MagicMock(last_status="ok")]
    assert get_overall_status(checks) == "operational"


def test_get_overall_status_degraded():
    checks = [MagicMock(last_status="ok"), MagicMock(last_status="degraded")]
    assert get_overall_status(checks) == "degraded"


def test_get_overall_status_outage():
    checks = [MagicMock(last_status="degraded"), MagicMock(last_status="down")]
    assert get_overall_status(checks) == "outage"


def test_get_overall_status_empty():
    assert get_overall_status([]) == "operational"


# ---------------------------------------------------------------------------
# Test 8 — SLA target constants are correct
# ---------------------------------------------------------------------------


def test_sla_constants():
    assert API_SLA_PCT == 99.9
    assert DB_SLA_PCT == 99.95
    assert JOBS_SLA_PCT == 99.5
    assert RESPONSE_P95_MS == 500.0
