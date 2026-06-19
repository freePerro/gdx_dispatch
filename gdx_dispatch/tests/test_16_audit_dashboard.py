"""
Tests for gdx_dispatch/core/audit_dashboard.py — audit log viewer + SOC2 compliance dashboard.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, _payload_json

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-secret")
    # Patch the module-level ADMIN_TOKEN constant so already-imported module picks it up
    import gdx_dispatch.core.audit_dashboard as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "test-admin-secret")


@pytest.fixture
def db_session():
    """Fresh in-memory SQLite DB with audit_log table."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    yield db
    db.close()
    engine.dispose()


def _make_entry(event_type="user_created", actor_id="admin", entity_type="user", entity_id="e1", payload=None, prev_hash="0" * 64):
    """Build a valid AuditLog entry with correct hash."""
    payload = payload or {}
    actor = actor_id or "system"
    digest = hashlib.sha256(
        f"{prev_hash}{event_type}{actor}{entity_id}{_payload_json(payload)}".encode()
    ).hexdigest()
    return AuditLog(
        id=uuid.uuid4(),
        event_type=event_type,
        actor_id=actor_id,
        actor_role="admin",
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
        hash=digest,
        prev_hash=prev_hash,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def client_with_db(db_session):
    """TestClient backed by a real in-memory DB via dependency override."""
    from fastapi import FastAPI

    from gdx_dispatch.core.audit_dashboard import router as audit_dashboard_router
    from gdx_dispatch.core.database import get_db

    app = FastAPI()
    app.include_router(audit_dashboard_router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False), db_session


ADMIN_HEADERS = {"Authorization": "Bearer test-admin-secret"}
NON_ADMIN_HEADERS = {"Authorization": "Bearer wrong-token"}


# ---------------------------------------------------------------------------
# Test 1: Audit log list is admin-only
# ---------------------------------------------------------------------------


def test_audit_log_list_admin_only(client_with_db):
    client, _ = client_with_db
    r = client.get("/api/admin/audit-logs", headers=NON_ADMIN_HEADERS)
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"


# ---------------------------------------------------------------------------
# Test 2: Integrity check passes on a fresh (valid) log
# ---------------------------------------------------------------------------


def test_integrity_check_passes_on_fresh_log(client_with_db):
    client, db = client_with_db

    # Insert two chained entries
    e1 = _make_entry(entity_id="tenant-1", prev_hash="0" * 64)
    db.add(e1)
    db.flush()

    e2 = _make_entry(event_type="job_created", entity_id="tenant-1", prev_hash=e1.hash)
    db.add(e2)
    db.commit()

    r = client.get("/api/admin/audit-logs/integrity-check?tenant_id=tenant-1", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["broken_at_row"] is None
    assert data["total_rows"] == 2


# ---------------------------------------------------------------------------
# Test 3: Compliance summary has the right structure
# ---------------------------------------------------------------------------


def test_compliance_summary_structure(client_with_db):
    client, _ = client_with_db
    r = client.get("/api/admin/compliance-summary", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    required_keys = [
        "mfa_adoption_pct",
        "audit_log_integrity",
        "last_backup_age_hours",
        "active_session_count",
        "failed_login_24h",
        "tenants_with_kb_updates",
    ]
    for key in required_keys:
        assert key in data, f"Missing key: {key}"
    assert isinstance(data["mfa_adoption_pct"], float)
    assert isinstance(data["audit_log_integrity"], bool)
    assert isinstance(data["active_session_count"], int)
    assert isinstance(data["failed_login_24h"], int)
    assert isinstance(data["tenants_with_kb_updates"], int)


# ---------------------------------------------------------------------------
# Test 4: Export CSV returns proper CSV
# ---------------------------------------------------------------------------


def test_export_csv(client_with_db):
    client, db = client_with_db

    e1 = _make_entry(event_type="login", entity_id="tenant-abc")
    db.add(e1)
    db.commit()

    r = client.get("/api/admin/compliance-report?fmt=csv", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "compliance_report.csv" in r.headers.get("content-disposition", "")
    text = r.text
    assert "id" in text  # header row
    assert "event_type" in text


# ---------------------------------------------------------------------------
# Test 5: Non-admin is blocked from compliance-summary
# ---------------------------------------------------------------------------


def test_non_admin_blocked(client_with_db):
    client, _ = client_with_db
    for path in [
        "/api/admin/audit-logs",
        "/api/admin/audit-logs/integrity-check",
        "/api/admin/compliance-summary",
        "/api/admin/compliance-report",
    ]:
        r = client.get(path, headers=NON_ADMIN_HEADERS)
        assert r.status_code == 403, f"Expected 403 on {path}, got {r.status_code}"


# ---------------------------------------------------------------------------
# Test 6: Tenant filter returns only matching rows
# ---------------------------------------------------------------------------


def test_tenant_filter_works(client_with_db):
    client, db = client_with_db

    e_t1 = _make_entry(entity_id="tenant-AAA-job-1", event_type="job_created")
    e_t2 = _make_entry(entity_id="tenant-BBB-job-2", event_type="job_created")
    db.add(e_t1)
    db.add(e_t2)
    db.commit()

    r = client.get("/api/admin/audit-logs?tenant_id=tenant-AAA", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert all("tenant-AAA" in ev["entity_id"] for ev in data["events"]), (
        f"Unexpected events returned: {[ev['entity_id'] for ev in data['events']]}"
    )
    # Should not include tenant-BBB rows
    assert all("tenant-BBB" not in ev["entity_id"] for ev in data["events"])
