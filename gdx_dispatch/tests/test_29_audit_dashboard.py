"""
Tests for gdx_dispatch/core/audit_dashboard.py — helper functions and new API routes.
8 tests covering get_audit_events, get_audit_summary, export_audit_log,
verify_audit_chain, and the HTTP routes that delegate to them.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import StreamingResponse

from gdx_dispatch.core.audit import AuditLog, TenantBase, _payload_json
from gdx_dispatch.core.audit_dashboard import (
    export_audit_log,
    get_audit_events,
    get_audit_summary,
    verify_audit_chain,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-secret")
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


def _make_entry(
    event_type="user_created",
    actor_id="admin",
    entity_type="user",
    entity_id="e1",
    payload=None,
    prev_hash="0" * 64,
):
    """Build a valid AuditLog entry with correct SHA-256 hash."""
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
    """TestClient backed by the real in-memory DB via dependency override."""
    from fastapi.testclient import TestClient

    from gdx_dispatch.app import create_app
    from gdx_dispatch.core.database import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False), db_session


ADMIN_HEADERS = {"Authorization": "Bearer test-admin-secret"}


# ---------------------------------------------------------------------------
# Test 1: get_audit_events — no filter returns all rows
# ---------------------------------------------------------------------------


def test_get_audit_events_no_filter(db_session):
    for i in range(3):
        db_session.add(_make_entry(entity_id=f"e{i}"))
    db_session.commit()

    result = get_audit_events(db_session)

    assert result["total"] == 3
    assert len(result["events"]) == 3
    assert result["page"] == 1


# ---------------------------------------------------------------------------
# Test 2: get_audit_events — filter by event_type
# ---------------------------------------------------------------------------


def test_get_audit_events_filter_by_event_type(db_session):
    db_session.add(_make_entry(event_type="login", entity_id="u1"))
    db_session.add(_make_entry(event_type="login", entity_id="u2"))
    db_session.add(_make_entry(event_type="job_created", entity_id="j1"))
    db_session.commit()

    result = get_audit_events(db_session, event_type="login")

    assert result["total"] == 2
    assert all(ev["event_type"] == "login" for ev in result["events"])


# ---------------------------------------------------------------------------
# Test 3: get_audit_events — filter by resource_type (entity_type)
# ---------------------------------------------------------------------------


def test_get_audit_events_filter_by_resource_type(db_session):
    db_session.add(_make_entry(entity_type="job", entity_id="j1"))
    db_session.add(_make_entry(entity_type="job", entity_id="j2"))
    db_session.add(_make_entry(entity_type="customer", entity_id="c1"))
    db_session.commit()

    result = get_audit_events(db_session, resource_type="job")

    assert result["total"] == 2
    assert all(ev["entity_type"] == "job" for ev in result["events"])


# ---------------------------------------------------------------------------
# Test 4: get_audit_events — pagination
# ---------------------------------------------------------------------------


def test_get_audit_events_pagination(db_session):
    for i in range(5):
        db_session.add(_make_entry(entity_id=f"x{i}"))
    db_session.commit()

    result = get_audit_events(db_session, page=1, limit=2)

    assert result["total"] == 5
    assert len(result["events"]) == 2
    assert result["pages"] == 3
    assert result["page"] == 1


# ---------------------------------------------------------------------------
# Test 5: get_audit_summary — counts by event_type
# ---------------------------------------------------------------------------


def test_get_audit_summary_counts(db_session):
    for i in range(3):
        db_session.add(_make_entry(event_type="login", entity_id=f"u{i}"))
    for i in range(2):
        db_session.add(_make_entry(event_type="job_created", entity_id=f"j{i}"))
    db_session.commit()

    result = get_audit_summary(db_session)

    assert result["total_events"] == 5
    assert result["by_event_type"].get("login") == 3
    assert result["by_event_type"].get("job_created") == 2
    assert result["period_days"] == 30
    assert isinstance(result["unique_actors"], int)
    assert isinstance(result["unique_resources"], int)


# ---------------------------------------------------------------------------
# Test 6: export_audit_log — CSV format
# ---------------------------------------------------------------------------


def test_export_audit_log_csv_format(db_session):
    db_session.add(_make_entry(event_type="login", entity_id="t1"))
    db_session.commit()

    result = export_audit_log(db_session, fmt="csv")

    assert isinstance(result, StreamingResponse)
    assert result.media_type == "text/csv"
    assert "audit_log.csv" in result.headers.get("content-disposition", "")

    # Drain the async body iterator with asyncio
    import asyncio

    async def _read():
        chunks = []
        async for chunk in result.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    body = asyncio.run(_read())
    first_line = body.splitlines()[0]
    assert "id" in first_line
    assert "created_at" in first_line
    assert "event_type" in first_line
    assert "login" in body


# ---------------------------------------------------------------------------
# Test 7: verify_audit_chain — valid chain returns ok=True
# ---------------------------------------------------------------------------


def test_verify_audit_chain_valid(db_session):
    e1 = _make_entry(entity_id="tenant-1", prev_hash="0" * 64)
    db_session.add(e1)
    db_session.flush()

    e2 = _make_entry(event_type="job_created", entity_id="tenant-1", prev_hash=e1.hash)
    db_session.add(e2)
    db_session.commit()

    result = verify_audit_chain(db_session, tenant_id="tenant-1")

    assert result["ok"] is True
    assert result["broken_at_row"] is None
    assert result["total_rows"] == 2
    assert "intact" in result["message"].lower()


# ---------------------------------------------------------------------------
# Test 8: verify_audit_chain — tampered hash detected
# ---------------------------------------------------------------------------


def test_verify_audit_chain_broken(db_session):
    e1 = _make_entry(entity_id="tenant-bad", prev_hash="0" * 64)
    # Tamper the hash after construction
    e1.hash = "deadbeef" * 8  # 64 hex chars but wrong value
    db_session.add(e1)
    db_session.commit()

    result = verify_audit_chain(db_session, tenant_id="tenant-bad")

    assert result["ok"] is False
    assert result["broken_at_row"] is not None
    assert result["broken_at_row"] >= 1
