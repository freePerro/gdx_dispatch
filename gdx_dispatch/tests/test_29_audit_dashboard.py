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


# ---------------------------------------------------------------------------
# Plan §13: the on-demand /api/audit/verify-chain endpoint + nightly task.
# The chain was tamper-evident but NEVER verified outside tests — no endpoint,
# no schedule. These pin both entry points, seeding via the REAL
# log_audit_event_sync so the core hash chain the endpoint verifies is valid.
# ---------------------------------------------------------------------------


def _seed_real_chain(db, n=3):
    from gdx_dispatch.core.audit import log_audit_event_sync

    for i in range(n):
        log_audit_event_sync(
            db, tenant_id="tenant-1", user_id=f"u{i}",
            action="job_closeout", entity_type="job", entity_id=f"job-{i}",
            details={"i": i},
        )
    db.commit()


def test_verify_chain_endpoint_ok(db_session):
    from gdx_dispatch.routers.audit import verify_audit_chain_endpoint

    _seed_real_chain(db_session, 3)
    res = verify_audit_chain_endpoint(entity_type=None, entity_id=None, _={"role": "admin"}, db=db_session)
    assert res["ok"] is True
    assert res["rows_checked"] >= 3
    assert res["scope"] == "all"
    assert res["unchained_rows"] == 0
    assert res["tamper_suspected"] is False


def test_verify_chain_endpoint_detects_tamper(db_session):
    import datetime
    import uuid

    from sqlalchemy import text as _text

    from gdx_dispatch.routers.audit import verify_audit_chain_endpoint

    _seed_real_chain(db_session, 3)
    # audit_logs is IMMUTABLE (append-only enforced), so a row cannot be
    # UPDATE-tampered — that immutability IS the guarantee. A realistic break
    # is a mis-linked INSERT: a row whose prev_hash/row_hash don't chain. Raw
    # SQL because the ORM path computes correct hashes.
    db_session.execute(
        _text(
            "INSERT INTO audit_logs (id, tenant_id, user_id, action, entity_type, "
            "entity_id, details, prev_hash, row_hash, hash, created_at) VALUES "
            "(:id, 'tenant-1', 'x', 'job_closeout', 'job', 'jX', '{}', "
            "'BADPREV', 'BADHASH', 'BADHASH', :ts)"
        ),
        {"id": uuid.uuid4().hex, "ts": datetime.datetime.now(datetime.UTC).isoformat()},
    )
    db_session.commit()

    res = verify_audit_chain_endpoint(entity_type=None, entity_id=None, _={"role": "admin"}, db=db_session)
    assert res["ok"] is False
    # The mis-linked row carries a (wrong) non-empty hash, so nothing is
    # "unchained" — this reads as a genuine tamper, not a data-hygiene gap.
    assert res["unchained_rows"] == 0
    assert res["tamper_suspected"] is True


def _seed_unchained_row(db):
    """A row written OUTSIDE the chain (empty row_hash), like the GL writers."""
    import datetime
    import uuid

    from sqlalchemy import text as _t
    db.execute(
        _t("INSERT INTO audit_logs (id, tenant_id, user_id, action, entity_type, "
           "entity_id, details, prev_hash, row_hash, hash, created_at) VALUES "
           "(:id,'tenant-1','gl','gl_posted','ledger','x','{}','','','',:ts)"),
        {"id": uuid.uuid4().hex, "ts": datetime.datetime.now(datetime.UTC).isoformat()},
    )
    db.commit()


def test_unchained_rows_read_as_data_hygiene_not_tamper(db_session):
    """A GL-style empty-hash row breaks verify but is NOT tampering — the
    endpoint must distinguish it (unchained_rows>0, tamper_suspected False)."""
    from gdx_dispatch.routers.audit import verify_audit_chain_endpoint

    _seed_real_chain(db_session, 2)
    _seed_unchained_row(db_session)
    res = verify_audit_chain_endpoint(entity_type=None, entity_id=None, _={"role": "admin"}, db=db_session)
    assert res["ok"] is False
    assert res["unchained_rows"] >= 1
    assert res["tamper_suspected"] is False


def test_nightly_task_logs_and_returns(db_session, monkeypatch):
    """The beat task returns {ok, rows} and never raises (a scheduled integrity
    check must not crash the beat)."""
    import gdx_dispatch.tasks.audit_chain_verify as mod

    class _Ctx:
        def __enter__(self):
            return db_session

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "SessionLocal", lambda: _Ctx())
    _seed_real_chain(db_session, 2)

    out = mod.verify_chain_nightly()
    assert out["ok"] is True
    assert out["rows"] >= 2
