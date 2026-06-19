"""SS-10 Slice L — PG round-trip proof for sandbox admin ``actor_role``.

The SS-10 Slice J unit suite (``gdx_dispatch/tests/test_sandbox_admin_router.py``)
asserts that ``log_audit_event_sync(... actor_role=...)`` writes the
expected role on SQLite. This integration test exercises the same
sandbox-admin mutation paths against a real Postgres instance under the
SS-5 PG gate, so the ``actor_role`` column round-trips through PG's
typed schema and the per-tenant ``audit_logs`` table created by
``ensure_audit_table``.

Skipped silently on the default SQLite test run.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
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
from gdx_dispatch.routers import sandbox_admin
from gdx_dispatch.routers.auth import get_current_user

PG_URL = os.environ.get("GDX_TEST_CONTROL_DB_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="SS-10 Slice L PG tests require GDX_TEST_CONTROL_DB_URL",
)

_TENANT_ID = "acme-co"


@pytest.fixture
def pg_session_factory():
    """Build a PG-backed session factory for the sandbox_admin router.

    The PG gate has already run ``alembic upgrade head`` against this DB,
    which leaves the platform ``audit_logs`` shape in place — that
    schema is missing columns the per-tenant ``TenantBase.AuditLog``
    needs (``tenant_id``, ``user_id``, ``action``, ``details``,
    ``row_hash``, etc.). Drop and recreate ``audit_logs`` with the full
    per-tenant column set so ``log_audit_event_sync`` can insert into it.

    A standalone ``sandbox_envs`` table without the ``tenants.slug`` FK
    keeps the test isolated from the platform FK graph; the router does
    not depend on the FK and we do not seed a tenant row.
    """
    eng = create_engine(PG_URL, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS audit_logs CASCADE"))
        conn.execute(text("""
            CREATE TABLE audit_logs (
                id UUID PRIMARY KEY,
                tenant_id TEXT,
                user_id TEXT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                details JSONB,
                ip_address TEXT,
                request_id TEXT,
                row_hash TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                event_type TEXT,
                actor_id TEXT,
                actor_role TEXT,
                payload JSONB,
                hash TEXT
            )
        """))
        conn.execute(text("DROP TABLE IF EXISTS sandbox_envs CASCADE"))
        conn.execute(text("""
            CREATE TABLE sandbox_envs (
                id UUID PRIMARY KEY,
                tenant_id VARCHAR(100) NOT NULL,
                subdomain VARCHAR(128) NOT NULL UNIQUE,
                status VARCHAR(32) NOT NULL DEFAULT 'provisioning',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_reset_at TIMESTAMPTZ,
                torn_down_at TIMESTAMPTZ
            )
        """))

    _AUDIT_GUARD_INITIALIZED.discard(id(eng))

    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

    pre_warm = factory()
    try:
        ensure_audit_table(pre_warm)
    finally:
        pre_warm.close()

    try:
        yield factory, eng
    finally:
        with eng.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS audit_logs CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS sandbox_envs CASCADE"))
            conn.execute(text(
                "DROP FUNCTION IF EXISTS audit_logs_immutable_guard() CASCADE"
            ))
        _AUDIT_GUARD_INITIALIZED.discard(id(eng))
        eng.dispose()


def _make_client(factory, *, role: str):
    app = FastAPI()
    app.include_router(sandbox_admin.router)

    commit_calls = {"count": 0}

    def override_get_db():
        sess = factory()

        @event.listens_for(sess, "after_commit")
        def _on_commit(_s):
            commit_calls["count"] += 1

        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_module("jobs")] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "u1",
        "tenant_id": _TENANT_ID,
        "role": role,
    }

    user_role = role

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = {
                "id": _TENANT_ID,
                "db_url": PG_URL,
                "name": "Acme",
            }
            request.state.current_user = {
                "user_id": "u1",
                "tenant_id": _TENANT_ID,
                "role": user_role,
            }
            return await call_next(request)

    app.add_middleware(TenantMiddleware)
    return TestClient(app), commit_calls


def _audit_actor_role_for(factory, *, action: str) -> str | None:
    sess = factory()
    try:
        row = sess.execute(
            select(AuditLog).where(AuditLog.action == action)
        ).scalar_one()
        return row.actor_role
    finally:
        sess.close()


def _seed_sandbox(factory, *, subdomain: str) -> str:
    """Insert a SandboxEnv row directly so reset/teardown have a target."""
    sess = factory()
    try:
        sandbox_id = str(uuid4())
        sess.execute(
            text("""
                INSERT INTO sandbox_envs (id, tenant_id, subdomain, status)
                VALUES (:id, :tenant_id, :subdomain, 'active')
            """),
            {"id": sandbox_id, "tenant_id": _TENANT_ID, "subdomain": subdomain},
        )
        sess.commit()
        return sandbox_id
    finally:
        sess.close()


@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_pg_provision_persists_actor_role(role, pg_session_factory):
    factory, _ = pg_session_factory
    client, commit_calls = _make_client(factory, role=role)

    resp = client.post(
        "/api/admin/sandbox", json={"subdomain": f"pg-prov-{role}"}
    )
    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    assert _audit_actor_role_for(factory, action="sandbox_provisioned") == role


@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_pg_reset_persists_actor_role(role, pg_session_factory):
    factory, _ = pg_session_factory
    sandbox_id = _seed_sandbox(factory, subdomain=f"pg-reset-{role}")
    client, commit_calls = _make_client(factory, role=role)

    resp = client.post(f"/api/admin/sandbox/{sandbox_id}/reset")
    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    assert _audit_actor_role_for(factory, action="sandbox_reset") == role


@pytest.mark.parametrize("role", ["admin", "owner", "superadmin"])
def test_pg_teardown_persists_actor_role(role, pg_session_factory):
    factory, _ = pg_session_factory
    sandbox_id = _seed_sandbox(factory, subdomain=f"pg-tear-{role}")
    client, commit_calls = _make_client(factory, role=role)

    resp = client.delete(f"/api/admin/sandbox/{sandbox_id}")
    assert resp.status_code == 200, resp.text
    assert commit_calls["count"] == 1

    assert _audit_actor_role_for(factory, action="sandbox_torn_down") == role
