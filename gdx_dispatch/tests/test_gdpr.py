"""
Tests for gdx_dispatch/routers/gdpr.py — GDPR + CCPA data-rights endpoints.

Uses ORM-based test fixtures with TenantBase.metadata.create_all() for
schema creation. Extra columns (sms_opt_out, email_opt_out) added via
ALTER TABLE since they aren't on the Customer model yet.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

USER_1 = "aaaaaaaa-0000-0000-0000-000000000001"

import pytest

pytestmark = pytest.mark.routers
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Job, User
from gdx_dispatch.routers.gdpr import (
    ccpa_opt_out,
    delete_customer_gdpr,
    export_customer,
    export_my_data,
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _mock_request(tenant_id: str = TENANT_A) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": tenant_id}),
        headers={},
        client=None,
    )


def _mock_user(role: str = "admin", sub: str = USER_1, email: str = "u@example.com") -> dict:
    return {"sub": sub, "user_id": sub, "tenant_id": TENANT_A, "role": role, "email": email}


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    # Add CCPA columns not yet on the Customer ORM model
    try:
        db.execute(text("ALTER TABLE customers ADD COLUMN sms_opt_out BOOLEAN DEFAULT 0"))
        db.execute(text("ALTER TABLE customers ADD COLUMN email_opt_out BOOLEAN DEFAULT 0"))
        db.commit()
    except Exception:
        db.rollback()  # columns may already exist

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_customer(
    db,
    *,
    tenant_id: str = TENANT_A,
    name: str = "Jane Doe",
    email: str | None = "jane@example.com",
    phone: str | None = "555-1234",
    address: str | None = "1 Privacy Ln",
) -> str:
    cust = Customer(
        name=name,
        email=email,
        phone=phone,
        address=address,
        customer_type="Retail",
        company_id=tenant_id,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return str(cust.id)


def _seed_user(db, uid: str = USER_1, email: str = "u@example.com", role: str = "admin") -> None:
    user = User(
        id=uuid.UUID(uid),
        email=email,
        role=role,
        company_id="tenant-test",
    )
    db.add(user)
    db.commit()


# ---------------------------------------------------------------------------
# 1. Self-export
# ---------------------------------------------------------------------------

def test_export_my_data_returns_user_profile(tenant_db_session):
    _seed_user(tenant_db_session, uid=USER_1, email="u@example.com")
    # Seed a job assigned to this user
    job = Job(
        title="Fix door",
        lifecycle_stage="scheduled",
        assigned_to=USER_1,
        company_id=TENANT_A,
    )
    tenant_db_session.add(job)
    tenant_db_session.commit()

    out = export_my_data(
        request=_mock_request(),
        user=_mock_user(sub=USER_1, email="u@example.com"),
        db=tenant_db_session,
    )

    assert "exported_at" in out
    assert out["user"]["user_id"] == USER_1
    assert out["user"].get("email") == "u@example.com"
    assert len(out["data"]["jobs"]) == 1
    assert out["data"]["jobs"][0]["title"] == "Fix door"
    assert isinstance(out["data"]["audit_events"], list)


# ---------------------------------------------------------------------------
# 2. Admin gate on export-customer
# ---------------------------------------------------------------------------

def test_export_customer_admin_path_succeeds(tenant_db_session):
    """The admin role check is enforced as a router Depends; at the handler level
    we verify the handler returns data for a real customer + tenant."""
    cid = _seed_customer(tenant_db_session, name="Carl", email="carl@example.com")

    out = export_customer(
        customer_id=uuid.UUID(cid),
        request=_mock_request(),
        user=_mock_user(role="admin"),
        db=tenant_db_session,
    )
    assert out["customer"]["id"] == cid
    assert out["customer"]["name"] == "Carl"
    assert "jobs" in out["data"]
    assert "audit_events" in out["data"]


def test_export_customer_requires_admin():
    """require_role('admin', 'owner') dependency must be attached to the route."""
    from gdx_dispatch.routers.gdpr import router

    route = next(r for r in router.routes if "/api/gdpr/export-customer/" in getattr(r, "path", ""))
    # Router-level dep + route-level dep. Find a require_role dep in the chain.
    dep_calls = [d.call for d in route.dependant.dependencies]
    # At least one should be the require_role closure (function named '_dependency')
    found_role_gate = any(
        getattr(c, "__qualname__", "").endswith("require_role.<locals>._dependency")
        or getattr(c, "__name__", "") == "_dependency"
        for c in dep_calls
    )
    assert found_role_gate, f"No require_role dependency found on route: {dep_calls}"


# ---------------------------------------------------------------------------
# 3. Delete customer redacts PII
# ---------------------------------------------------------------------------

def test_delete_customer_redacts_pii(tenant_db_session):
    cid = _seed_customer(
        tenant_db_session,
        name="Original Name",
        email="victim@example.com",
        phone="555-9999",
        address="1 Secret Rd",
    )

    out = delete_customer_gdpr(
        customer_id=uuid.UUID(cid),
        request=_mock_request(),
        user=_mock_user(role="admin"),
        db=tenant_db_session,
    )
    assert out["ok"] is True

    # Re-fetch via ORM to check redacted values (Customer PII is plain Text post-S122-1c)
    from sqlalchemy import select
    row = tenant_db_session.execute(
        select(Customer).where(Customer.id == uuid.UUID(cid))
    ).scalar_one_or_none()
    assert row is not None
    assert row.name == "Redacted"
    assert row.email is None
    assert row.phone is None
    assert row.address is None
    assert row.deleted_at is not None

    # Audit event recorded
    audit_row = tenant_db_session.execute(
        text("SELECT action, entity_id FROM audit_logs WHERE action = 'gdpr_customer_deleted'"),
    ).mappings().first()
    assert audit_row is not None
    assert audit_row["entity_id"] == cid


def test_delete_customer_not_found(tenant_db_session):
    with pytest.raises(HTTPException) as exc:
        delete_customer_gdpr(
            customer_id=uuid.uuid4(),
            request=_mock_request(),
            user=_mock_user(role="admin"),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 4. CCPA opt-out
# ---------------------------------------------------------------------------

def test_ccpa_opt_out_sets_flags(tenant_db_session, monkeypatch):
    cid = _seed_customer(tenant_db_session, name="Opt Out Oscar")

    # Patch db.execute to intercept the information_schema query (PostgreSQL-only)
    # and return a result indicating the column exists (SQLite has it via ALTER TABLE above)
    _original_execute = tenant_db_session.execute

    def _patched_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        if "information_schema" in stmt_str:
            # Return a fake result that indicates the column exists
            from unittest.mock import MagicMock
            result = MagicMock()
            result.fetchone.return_value = ("sms_opt_out",)
            return result
        return _original_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(tenant_db_session, "execute", _patched_execute)

    out = ccpa_opt_out(
        customer_id=uuid.UUID(cid),
        request=_mock_request(),
        user=_mock_user(role="admin"),
        db=tenant_db_session,
    )
    assert out["sms_opt_out"] is True
    assert out["email_opt_out"] is True

    # Verify return value indicates opt-out was set
    assert out["sms_opt_out"] is True
    assert out["email_opt_out"] is True
    assert out["ok"] is True

    # Verify audit event was recorded
    audit_row = tenant_db_session.execute(
        text("SELECT action FROM audit_logs WHERE action = 'ccpa_opt_out'"),
    ).mappings().first()
    assert audit_row is not None


# ---------------------------------------------------------------------------
# 5. Tenant scope isolation
# ---------------------------------------------------------------------------

def test_tenant_scope(tenant_db_session):
    # Customer belongs to tenant A
    cid_a = _seed_customer(tenant_db_session, tenant_id=TENANT_A, name="Tenant A Customer")

    # Attempt to export from tenant B context -> 404
    with pytest.raises(HTTPException) as exc:
        export_customer(
            customer_id=uuid.UUID(cid_a),
            request=_mock_request(tenant_id=TENANT_B),
            user=_mock_user(role="admin"),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 404

    # Same from delete
    with pytest.raises(HTTPException) as exc:
        delete_customer_gdpr(
            customer_id=uuid.UUID(cid_a),
            request=_mock_request(tenant_id=TENANT_B),
            user=_mock_user(role="admin"),
            db=tenant_db_session,
        )
    assert exc.value.status_code == 404
