"""Phase D — cc2-s49a tenant-side support submissions.

Tests the /api/support/bug and /api/support/feature endpoints in
``gdx_dispatch/routers/support.py``. PG-only because the writes land in
``cc_support_tickets`` (control plane).
"""
from __future__ import annotations

import os
from collections.abc import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

if not os.environ.get("GDX_TEST_CONTROL_DB_URL", "").strip():
    pytest.skip(
        "support tests require GDX_TEST_CONTROL_DB_URL pointing at a Postgres "
        "pre-loaded with cc-v2 alembic head (>= 064_cc_support_tickets)",
        allow_module_level=True,
    )


@pytest.fixture
def app(control_db: Session) -> FastAPI:
    """Minimal FastAPI app mounting only the support router + a dummy
    middleware that injects ``request.state.tenant``. Avoids loading
    the full gdx app (which has ~80 routers and module-level work)."""
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user
    from gdx_dispatch.routers.support import router

    app = FastAPI()
    app.include_router(router)

    # Override get_current_user with a fixture-style dummy.
    def _stub_user() -> dict:
        return {
            "sub": "00000000-0000-0000-0000-000000000aaa",
            "email": "submitter@example.com",
        }

    def _stub_db() -> Generator[Session, None, None]:
        yield control_db

    app.dependency_overrides[get_current_user] = _stub_user
    app.dependency_overrides[get_db] = _stub_db

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        # Pin a known tenant id; tests will seed a row with this id.
        request.state.tenant = {"id": _STUB_TENANT_ID}
        return await call_next(request)

    return app


_STUB_TENANT_ID = "11111111-1111-1111-1111-111111111aaa"


@pytest.fixture
def client_with_tenant(app: FastAPI, control_db: Session) -> Generator[TestClient, None, None]:
    """Seed the stub tenant + yield a TestClient."""
    control_db.execute(
        sa_text(
            "INSERT INTO tenants (id, slug, name, db_provisioned, "
            " subscription_status, timezone, created_at) "
            "VALUES (:id, :slug, 'Stub Tenant', true, 'active', "
            "        'America/New_York', now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": _STUB_TENANT_ID, "slug": f"phd-s49a-{uuid4().hex[:8]}"},
    )
    control_db.flush()
    yield TestClient(app)


def test_submit_bug_happy_path(client_with_tenant, control_db):
    r = client_with_tenant.post(
        "/api/support/bug",
        json={
            "subject": "App crashes on save",
            "body": "Reproduces every time on the new dispatch board.",
            "priority": "high",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    assert "ticket_id" in body

    row = control_db.execute(
        sa_text(
            "SELECT category, priority, opened_by_email, tenant_id "
            "FROM cc_support_tickets WHERE id = :id"
        ),
        {"id": body["ticket_id"]},
    ).first()
    assert row.category == "bug"
    assert row.priority == "high"
    assert row.opened_by_email == "submitter@example.com"
    assert str(row.tenant_id) == _STUB_TENANT_ID


def test_submit_feature_happy_path(client_with_tenant, control_db):
    r = client_with_tenant.post(
        "/api/support/feature",
        json={
            "subject": "Add bulk-edit to the dispatch lane",
            "body": "Would save us 15 minutes a day.",
        },
    )
    assert r.status_code == 201, r.text
    row = control_db.execute(
        sa_text(
            "SELECT category, priority FROM cc_support_tickets WHERE id = :id"
        ),
        {"id": r.json()["ticket_id"]},
    ).first()
    assert row.category == "feature"
    assert row.priority == "medium"  # default


def test_submit_invalid_priority_422(client_with_tenant):
    r = client_with_tenant.post(
        "/api/support/bug",
        json={"subject": "x" * 5, "body": "y" * 10, "priority": "EXTREME"},
    )
    assert r.status_code == 422


def test_submit_short_body_422(client_with_tenant):
    r = client_with_tenant.post(
        "/api/support/bug",
        json={"subject": "x" * 5, "body": "no"},  # body min_length=5
    )
    assert r.status_code == 422
