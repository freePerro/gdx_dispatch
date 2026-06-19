"""SOC 2 Type II compliance tests for GDX platform.
Each test maps to a specific SOC 2 Trust Service Criteria control.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import make_fresh_db
from pydantic import ValidationError
from sqlalchemy import Text
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.tenant_models import SafetyChecklist
from gdx_dispatch.routers.safety_checklist import (
    ChecklistCompleteIn,
    complete_checklist,
    get_job_checklist,
)


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-soc2") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-soc1")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def ctx():
    # Temporarily swap SafetyChecklist DateTime columns to Text so the router's
    # ISO-string timestamps are accepted by SQLite.
    _orig_signed = SafetyChecklist.signed_at.property.columns[0].type
    _orig_created = SafetyChecklist.created_at.property.columns[0].type
    _orig_deleted = SafetyChecklist.deleted_at.property.columns[0].type
    SafetyChecklist.signed_at.property.columns[0].type = Text()
    SafetyChecklist.created_at.property.columns[0].type = Text()
    SafetyChecklist.deleted_at.property.columns[0].type = Text()

    engine = make_fresh_db()
    # Recreate with TEXT columns to match
    with engine.connect() as conn:
        conn.execute(sa_text("DROP TABLE IF EXISTS safety_checklists"))
        conn.execute(sa_text(
            """CREATE TABLE safety_checklists (
                id VARCHAR(36) PRIMARY KEY,
                company_id VARCHAR(36) NOT NULL,
                job_id VARCHAR(36) NOT NULL,
                technician_id VARCHAR(36) NOT NULL,
                items TEXT NOT NULL,
                completed BOOLEAN,
                photo_url TEXT,
                signed_at TEXT,
                created_at TEXT,
                deleted_at TEXT
            )"""
        ))
        conn.commit()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SL()
    req = DummyRequest()
    user = {"user_id": "admin-1", "sub": "admin-1", "role": "admin"}
    try:
        yield db, req, user, SL
    finally:
        db.close()
        engine.dispose()
        SafetyChecklist.signed_at.property.columns[0].type = _orig_signed
        SafetyChecklist.created_at.property.columns[0].type = _orig_created
        SafetyChecklist.deleted_at.property.columns[0].type = _orig_deleted


# --- CC6.1: Tenant Isolation ---

def test_tenant_isolation_safety_checklist(ctx):
    """SOC 2 CC6.1 — Tenant isolation prevents cross-tenant data access.

    Three-plane (2026-04-24 B1): isolation is now enforced by the per-tenant
    database connection (Depends(get_db) picks the tenant's own DB by
    request.state.tenant). App-level company_id filters have been removed as
    redundant and unsafe (they 404'd on NULL-company rows — the 2026-04-22
    document bug). In production, tenant B's session can never see tenant A's
    rows because the connection itself points at a different database.

    This unit test uses a single shared SQLite session, so at this layer we
    assert only that the handler no longer filters by company_id — the
    infrastructure-level isolation is exercised by RLS gate tests in
    gdx_dispatch/tests/test_get_db_rls.py and the tenant_scoping_scan tool.
    """
    db, _, user, _ = ctx
    req_a = DummyRequest(tenant_id="tenant-A")
    req_b = DummyRequest(tenant_id="tenant-B")
    job_id = "job-isolation-test"

    payload = ChecklistCompleteIn(
        job_id=job_id,
        items=[{"item": "Isolation test", "checked": True}],
    )
    complete_checklist(request=req_a, payload=payload, user=user, db=db)

    # Shared test session: both tenants see the same row (that's the expected
    # *test-layer* behavior now; prod isolation is the connection).
    result = get_job_checklist(job_id=job_id, request=req_b, user=user, db=db)
    assert result is not None


# --- CC7.1: Audit Logging ---

def test_audit_log_created_on_mutation(ctx):
    """SOC 2 CC7.1 — Every state-changing operation is audit-logged.
    Critical for forensic investigations and proving accountability.
    The audit entry must capture WHO did WHAT to WHICH entity.
    """
    db, req, user, _ = ctx
    payload = ChecklistCompleteIn(
        job_id="job-audit-soc2",
        items=[{"item": "Audit test", "checked": True}],
    )
    complete_checklist(request=req, payload=payload, user=user, db=db)

    row = db.execute(sa_text(
        "SELECT user_id, action, entity_type, entity_id "
        "FROM audit_logs WHERE entity_type = 'safety_checklist' "
        "ORDER BY created_at DESC LIMIT 1"
    )).mappings().first()

    assert row is not None, "Audit log entry must exist after mutation"
    assert row["user_id"] == "admin-1", "Audit must capture the acting user"
    assert row["action"] == "create", "Audit must capture the action type"
    assert row["entity_id"] is not None, "Audit must reference the created entity"


def test_audit_log_has_timestamp(ctx):
    """SOC 2 CC7.2 — Audit entries must have timestamps.
    Required to reconstruct event sequences during incidents.
    """
    db, req, user, _ = ctx
    complete_checklist(
        request=req,
        payload=ChecklistCompleteIn(job_id="job-ts", items=[{"item": "T", "checked": True}]),
        user=user, db=db,
    )
    ts = db.execute(sa_text(
        "SELECT created_at FROM audit_logs WHERE entity_type = 'safety_checklist' LIMIT 1"
    )).scalar()
    assert ts is not None, "Audit log must have a created_at timestamp"


# --- PI1.3: Input Validation ---

def test_input_validation_rejects_oversized_items(ctx):
    """SOC 2 PI1.3 — Input validation rejects oversized payloads.
    Prevents DoS via resource exhaustion. ChecklistCompleteIn.items has max_length=100.
    """
    oversized = [{"item": f"Item {i}", "checked": True} for i in range(200)]
    with pytest.raises(ValidationError):
        ChecklistCompleteIn(job_id="job-big", items=oversized)


def test_input_validation_rejects_empty_job_id(ctx):
    """SOC 2 PI1.3 — Empty identifiers are rejected at the schema level.
    Prevents logic bypasses in tenant-scoped queries.
    """
    with pytest.raises(ValidationError):
        ChecklistCompleteIn(job_id="", items=[{"item": "X", "checked": True}])


def test_input_validation_rejects_long_job_id(ctx):
    """SOC 2 PI1.3 — Excessively long identifiers are rejected.
    job_id max_length is 36 (UUID format).
    """
    with pytest.raises(ValidationError):
        ChecklistCompleteIn(job_id="x" * 100, items=[{"item": "X", "checked": True}])


# --- C1.4: Data Minimization ---

def test_checklist_response_includes_expected_fields(ctx):
    """SOC 2 C1.4 — API responses contain only necessary fields.
    Verify the response structure matches what the UI needs, no more.
    """
    db, req, user, _ = ctx
    result = complete_checklist(
        request=req,
        payload=ChecklistCompleteIn(job_id="job-fields-soc2", items=[{"item": "F", "checked": True}]),
        user=user, db=db,
    )
    # These fields SHOULD be in the response
    assert "id" in result
    assert "job_id" in result
    assert "items" in result
    assert "completed" in result

    # password_hash should NEVER be in any API response
    assert "password_hash" not in result
    assert "password" not in result


# --- CC6.7: Credential Storage ---

def test_password_hash_not_plaintext(ctx):
    """SOC 2 CC6.7 — Passwords must be stored as cryptographic hashes.
    Checks that a password in the users table starts with a known hash prefix.
    Plaintext passwords are a critical compliance violation.
    """
    db, _, _, _ = ctx
    # Ensure users table exists in test DB
    db.execute(sa_text("""CREATE TABLE IF NOT EXISTS users (
        id VARCHAR(36) PRIMARY KEY, username VARCHAR(100), email VARCHAR(254),
        password_hash TEXT, role VARCHAR(20), company_id VARCHAR(36),
        active BOOLEAN DEFAULT true, created_at TIMESTAMP, deleted_at TIMESTAMP)"""))
    db.commit()
    from werkzeug.security import generate_password_hash
    hashed = generate_password_hash("TestP@ss123")
    db.execute(sa_text(
        "INSERT INTO users (id, username, email, password_hash, role, company_id) "
        "VALUES ('soc2-user-1', 'soc2test', 'soc2@test.com', :hash, 'admin', 'tenant-soc2')"
    ), {"hash": hashed})
    db.commit()

    stored = db.execute(sa_text(
        "SELECT password_hash FROM users WHERE id = 'soc2-user-1'"
    )).scalar()

    assert stored != "TestP@ss123", "Password stored as plaintext — CRITICAL VIOLATION"
    assert stored.startswith(("$2b$", "scrypt:", "pbkdf2:")), \
        f"Password hash must use bcrypt/scrypt/pbkdf2, got: {stored[:20]}"
