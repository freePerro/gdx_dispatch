"""Tenant-plane support submissions (/api/support).

Originally these endpoints wrote to the control-plane
``cc_support_tickets`` table and this file skipped unless a CC-provisioned
Postgres URL was exported — which no environment set, so the suite never
ran while prod bounced every submission with a 503 (2026-07-07 audit).
Tickets now land in the tenant-plane ``SupportTicket`` ORM model, so the
tests run everywhere on in-memory sqlite.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import SupportTicket

_STUB_TENANT_ID = "11111111-1111-1111-1111-111111111aaa"


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db: Session) -> TestClient:
    """Minimal app mounting only the support router + a middleware that
    injects ``request.state.tenant``. Avoids loading the full gdx app."""
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user
    from gdx_dispatch.routers.support import router

    app = FastAPI()
    app.include_router(router)

    def _stub_user() -> dict:
        return {
            "sub": "00000000-0000-0000-0000-000000000aaa",
            "email": "submitter@example.com",
        }

    def _stub_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_current_user] = _stub_user
    app.dependency_overrides[get_db] = _stub_db

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": _STUB_TENANT_ID}
        return await call_next(request)

    return TestClient(app)


def test_submit_bug_happy_path(client, db):
    r = client.post(
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

    row = db.get(SupportTicket, body["ticket_id"])
    assert row is not None
    assert row.category == "bug"
    assert row.priority == "high"
    assert row.status == "open"
    assert row.opened_by_email == "submitter@example.com"
    assert row.tenant_id == _STUB_TENANT_ID
    assert row.created_at is not None


def test_submit_feature_happy_path(client, db):
    r = client.post(
        "/api/support/feature",
        json={
            "subject": "Add bulk-edit to the dispatch lane",
            "body": "Would save us 15 minutes a day.",
        },
    )
    assert r.status_code == 201, r.text
    row = db.get(SupportTicket, r.json()["ticket_id"])
    assert row.category == "feature"
    assert row.priority == "medium"  # default


def test_submit_invalid_priority_422(client):
    r = client.post(
        "/api/support/bug",
        json={"subject": "x" * 5, "body": "y" * 10, "priority": "EXTREME"},
    )
    assert r.status_code == 422


def test_submit_short_body_422(client):
    r = client.post(
        "/api/support/bug",
        json={"subject": "x" * 5, "body": "no"},  # body min_length=5
    )
    assert r.status_code == 422


def test_my_lists_own_tenant_newest_first_with_category_filter(client, db):
    client.post(
        "/api/support/bug",
        json={"subject": "First bug", "body": "details details"},
    )
    client.post(
        "/api/support/feature",
        json={"subject": "A feature ask", "body": "details details"},
    )
    client.post(
        "/api/support/bug",
        json={"subject": "Second bug", "body": "details details"},
    )
    # A foreign tenant's ticket must never appear in /my.
    db.add(
        SupportTicket(
            id="99999999-9999-9999-9999-999999999999",
            tenant_id="22222222-2222-2222-2222-222222222bbb",
            opened_by_email="other@example.com",
            subject="Other tenant ticket",
            body="should not leak",
            category="bug",
            priority="low",
            status="open",
            created_at=datetime.now(UTC),
        )
    )
    db.commit()

    r = client.get("/api/support/my")
    assert r.status_code == 200, r.text
    subjects = [i["subject"] for i in r.json()["items"]]
    assert subjects == ["Second bug", "A feature ask", "First bug"]

    r = client.get("/api/support/my", params={"category": "feature"})
    assert [i["subject"] for i in r.json()["items"]] == ["A feature ask"]
