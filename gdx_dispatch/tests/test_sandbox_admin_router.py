"""SS-10 Slice F — tests for the sandbox admin router.

Covers role gating, tenant-context fail-closed behaviour, 404s for missing
sandbox rows, happy-path serialization, and the single-commit-per-request
contract. Slice H extends the suite to assert one audit row per successful
mutation, no audit row on 400/404 fail-closed paths, and that adding the
in-transaction audit write does not break the ``commit_count == 1`` contract.

Uses ``app.dependency_overrides`` so the tests focus on router logic and
dependency wiring rather than module/auth internals.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

import gdx_dispatch.models.platform  # noqa: F401 — register SS-2 tables
import gdx_dispatch.models.platform_extensions  # noqa: F401 — register SS-3 tables
from gdx_dispatch.core.audit import (
    _AUDIT_GUARD_INITIALIZED,
    AuditLog,
    ensure_audit_table,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.platform_extensions import SandboxEnv
from gdx_dispatch.routers import sandbox_admin
from gdx_dispatch.routers.auth import get_current_user

from uuid import UUID as _UUID_T
_TENANT_ID = _UUID_T("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Only create the table this router actually touches. Avoid
    # ``Base.metadata.create_all`` because the control-plane ``AuditLog``
    # in ``gdx_dispatch.models.platform_extensions`` and the per-tenant
    # ``gdx_dispatch.core.audit.AuditLog`` both bind to the table name
    # ``audit_logs`` with different column sets — creating the platform
    # variant first would leave ``log_audit_event_sync`` inserting into
    # a schema that lacks ``event_type``/``actor_id``/``row_hash``/etc.
    SandboxEnv.__table__.create(engine, checkfirst=True)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # Pre-warm the per-tenant ``audit_logs`` table + immutability triggers
    # in a session that does NOT carry the request-scoped ``after_commit``
    # listener, so the bookkeeping commit inside ``ensure_audit_table``
    # never bumps the request commit counter.
    pre_warm = factory()
    try:
        ensure_audit_table(pre_warm)
    finally:
        pre_warm.close()

    try:
        yield factory
    finally:
        engine.dispose()
        # ``ensure_audit_table`` keys its cache by the engine object (a
        # WeakSet); drop the entry so the next test re-installs the table +
        # triggers on its fresh engine.
        _AUDIT_GUARD_INITIALIZED.discard(engine)


def _make_client(
    session_factory,
    *,
    role: str | None = "admin",
    set_tenant: bool = True,
):
    """Build a TestClient for the sandbox_admin router with overrides.

    Returns ``(client, commit_calls)`` where ``commit_calls["count"]`` is
    the number of ``Session.commit()`` events observed across the request.
    """
    app = FastAPI()
    app.include_router(sandbox_admin.router)

    commit_calls = {"count": 0}

    def override_get_db():
        sess = session_factory()

        @event.listens_for(sess, "after_commit")
        def _on_commit(_s):
            commit_calls["count"] += 1

        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_module("jobs")] = lambda: None

    if role is not None:
        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": "u1",
            "tenant_id": _TENANT_ID,
            "role": role,
        }

    tenant_present = set_tenant
    user_role = role

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if tenant_present:
                request.state.tenant = {
                    "id": _TENANT_ID,
                    "db_url": "sqlite://",
                    "name": "Acme",
                }
            else:
                request.state.tenant = {}
            if user_role is not None:
                request.state.current_user = {
                    "user_id": "u1",
                    "tenant_id": _TENANT_ID,
                    "role": user_role,
                }
            return await call_next(request)

    app.add_middleware(TenantMiddleware)

    return TestClient(app), commit_calls


def _seed_sandbox(session_factory, *, subdomain: str = "seed-sbx"):
    """Persist a SandboxEnv row directly so reset/teardown have a target."""
    sess = session_factory()
    try:
        row = SandboxEnv(
            tenant_id=_TENANT_ID,
            subdomain=subdomain,
            status="active",
        )
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id
    finally:
        sess.close()


def _audit_rows(session_factory, *, action: str | None = None):
    """Return audit rows (optionally filtered by ``action``) ordered oldest first."""
    sess = session_factory()
    try:
        stmt = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return list(sess.execute(stmt).scalars().all())
    finally:
        sess.close()


# ── route registration ─────────────────────────────────────────────────────

def test_router_prefix_and_tag():
    assert sandbox_admin.router.prefix == "/api/admin/sandbox"
    assert "sandbox-admin" in sandbox_admin.router.tags


# ── role gating ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_admin_roles_can_provision(role, session_factory):
    client, _ = _make_client(session_factory, role=role)
    resp = client.post("/api/admin/sandbox", json={"subdomain": f"sbx-{role}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(_TENANT_ID)
    assert body["subdomain"] == f"sbx-{role}"
    assert body["status"] == "active"
    assert body["id"]


@pytest.mark.parametrize("role", ["technician", "dispatcher", "viewer"])
def test_non_admin_role_is_denied(role, session_factory):
    client, _ = _make_client(session_factory, role=role)
    resp = client.post("/api/admin/sandbox", json={"subdomain": "nope"})
    assert resp.status_code == 403


# ── missing tenant context ─────────────────────────────────────────────────

def test_missing_tenant_context_returns_400(session_factory):
    client, _ = _make_client(session_factory, role="admin", set_tenant=False)
    resp = client.post("/api/admin/sandbox", json={"subdomain": "sbx"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing tenant context"


# ── happy paths + commit count ─────────────────────────────────────────────

def test_provision_returns_serialized_row_and_commits_once(session_factory):
    client, commit_calls = _make_client(session_factory, role="admin")
    resp = client.post("/api/admin/sandbox", json={"subdomain": "happy-sbx"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"id", "tenant_id", "subdomain", "status"}
    assert body["tenant_id"] == str(_TENANT_ID)
    assert body["subdomain"] == "happy-sbx"
    assert body["status"] == "active"
    assert commit_calls["count"] == 1


def test_reset_returns_serialized_row_and_commits_once(session_factory):
    sandbox_id = _seed_sandbox(session_factory, subdomain="reset-sbx")
    client, commit_calls = _make_client(session_factory, role="owner")

    resp = client.post(f"/api/admin/sandbox/{sandbox_id}/reset")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"id", "status", "last_reset_at"}
    assert body["id"] == str(sandbox_id)
    assert body["status"] == "active"
    assert body["last_reset_at"] is not None
    assert commit_calls["count"] == 1


def test_teardown_returns_ok_and_commits_once(session_factory):
    sandbox_id = _seed_sandbox(session_factory, subdomain="teardown-sbx")
    client, commit_calls = _make_client(session_factory, role="superadmin")

    resp = client.delete(f"/api/admin/sandbox/{sandbox_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "id": str(sandbox_id)}
    assert commit_calls["count"] == 1


# ── not-found ──────────────────────────────────────────────────────────────

def test_reset_missing_sandbox_returns_404(session_factory):
    client, commit_calls = _make_client(session_factory, role="admin")
    resp = client.post(f"/api/admin/sandbox/{uuid4()}/reset")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Sandbox not found"
    assert commit_calls["count"] == 0


def test_teardown_missing_sandbox_returns_404(session_factory):
    client, commit_calls = _make_client(session_factory, role="admin")
    resp = client.delete(f"/api/admin/sandbox/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Sandbox not found"
    assert commit_calls["count"] == 0


# ── module gate is wired ───────────────────────────────────────────────────

def test_router_is_gated_by_jobs_module(session_factory):
    """Without the jobs-module override, the request 403s on module check.

    Confirms the router is actually wired through ``require_module("jobs")``.
    """
    app = FastAPI()
    app.include_router(sandbox_admin.router)

    def override_get_db():
        sess = session_factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "u1",
        "tenant_id": _TENANT_ID,
        "role": "admin",
    }

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = {
                "id": _TENANT_ID,
                "db_url": "sqlite://",
                "name": "Acme",
            }
            request.state.current_user = {
                "user_id": "u1",
                "tenant_id": _TENANT_ID,
                "role": "admin",
            }
            return await call_next(request)

    app.add_middleware(TenantMiddleware)

    resp = TestClient(app).post("/api/admin/sandbox", json={"subdomain": "sbx"})
    # No grant rows seeded → require_module("jobs") fails closed.
    assert resp.status_code == 403


def test_router_passes_with_jobs_module_overridden(session_factory):
    """Sanity check: with the override, the same payload succeeds."""
    client, _ = _make_client(session_factory, role="admin")
    resp = client.post("/api/admin/sandbox", json={"subdomain": "ok-sbx"})
    assert resp.status_code == 200, resp.text


# ── Slice H: audit logging on successful mutations ─────────────────────────

def test_provision_writes_audit_row(session_factory):
    client, commit_calls = _make_client(session_factory, role="admin")
    resp = client.post("/api/admin/sandbox", json={"subdomain": "audit-prov"})

    assert resp.status_code == 200, resp.text
    sandbox_id = resp.json()["id"]
    assert commit_calls["count"] == 1, "audit write must share the request commit"

    rows = _audit_rows(session_factory, action="sandbox_provisioned")
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == str(_TENANT_ID)
    assert row.user_id == "u1"
    assert row.entity_type == "sandbox_env"
    assert row.entity_id == sandbox_id
    assert (row.details or {}).get("subdomain") == "audit-prov"
    assert (row.details or {}).get("status") == "active"


def test_reset_writes_audit_row(session_factory):
    sandbox_id = _seed_sandbox(session_factory, subdomain="audit-reset")
    client, commit_calls = _make_client(session_factory, role="owner")

    resp = client.post(f"/api/admin/sandbox/{sandbox_id}/reset")

    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    rows = _audit_rows(session_factory, action="sandbox_reset")
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == str(_TENANT_ID)
    assert row.user_id == "u1"
    assert row.entity_type == "sandbox_env"
    assert row.entity_id == str(sandbox_id)
    assert (row.details or {}).get("status") == "active"
    assert (row.details or {}).get("last_reset_at") is not None


def test_teardown_writes_audit_row(session_factory):
    sandbox_id = _seed_sandbox(session_factory, subdomain="audit-tear")
    client, commit_calls = _make_client(session_factory, role="superadmin")

    resp = client.delete(f"/api/admin/sandbox/{sandbox_id}")

    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    rows = _audit_rows(session_factory, action="sandbox_torn_down")
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == str(_TENANT_ID)
    assert row.user_id == "u1"
    assert row.entity_type == "sandbox_env"
    assert row.entity_id == str(sandbox_id)
    assert (row.details or {}).get("status") == "torn_down"
    assert (row.details or {}).get("torn_down_at") is not None


# ── Slice H: fail-closed paths must NOT emit mutation audit rows ───────────

def test_missing_tenant_context_writes_no_audit_row(session_factory):
    client, _ = _make_client(session_factory, role="admin", set_tenant=False)
    resp = client.post("/api/admin/sandbox", json={"subdomain": "denied"})
    assert resp.status_code == 400
    assert _audit_rows(session_factory) == []


def test_reset_missing_sandbox_writes_no_audit_row(session_factory):
    client, _ = _make_client(session_factory, role="admin")
    resp = client.post(f"/api/admin/sandbox/{uuid4()}/reset")
    assert resp.status_code == 404
    assert _audit_rows(session_factory) == []


def test_teardown_missing_sandbox_writes_no_audit_row(session_factory):
    client, _ = _make_client(session_factory, role="admin")
    resp = client.delete(f"/api/admin/sandbox/{uuid4()}")
    assert resp.status_code == 404
    assert _audit_rows(session_factory) == []


def test_non_admin_role_writes_no_audit_row(session_factory):
    client, _ = _make_client(session_factory, role="technician")
    resp = client.post("/api/admin/sandbox", json={"subdomain": "denied"})
    assert resp.status_code == 403
    assert _audit_rows(session_factory) == []


# ── Slice J: actor_role populated on successful mutations ──────────────────

@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_provision_writes_expected_actor_role(role, session_factory):
    client, commit_calls = _make_client(session_factory, role=role)
    resp = client.post("/api/admin/sandbox", json={"subdomain": f"role-prov-{role}"})
    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    rows = _audit_rows(session_factory, action="sandbox_provisioned")
    assert len(rows) == 1
    assert rows[0].actor_role == role


@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_reset_writes_expected_actor_role(role, session_factory):
    sandbox_id = _seed_sandbox(session_factory, subdomain=f"role-reset-{role}")
    client, commit_calls = _make_client(session_factory, role=role)

    resp = client.post(f"/api/admin/sandbox/{sandbox_id}/reset")

    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    rows = _audit_rows(session_factory, action="sandbox_reset")
    assert len(rows) == 1
    assert rows[0].actor_role == role


@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_teardown_writes_expected_actor_role(role, session_factory):
    sandbox_id = _seed_sandbox(session_factory, subdomain=f"role-tear-{role}")
    client, commit_calls = _make_client(session_factory, role=role)

    resp = client.delete(f"/api/admin/sandbox/{sandbox_id}")

    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    rows = _audit_rows(session_factory, action="sandbox_torn_down")
    assert len(rows) == 1
    assert rows[0].actor_role == role
