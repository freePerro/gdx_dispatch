"""Tests for the inbound_comms router (Twilio SMS + email webhooks + admin)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.inbound_comms import admin_router, public_router


def _make_client(
    tenant_id: str = "tenant-test",
    user_sub: str = "user-1",
    engine=None,
) -> TestClient:
    if engine is None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'communications', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(public_router)
    app.include_router(admin_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user_sub,
        "sub": user_sub,
        "role": "admin",
        "tenant_id": tenant_id,
        "email": f"{user_sub}@example.com",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._session = Session  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def test_sms_webhook_creates_row(client: TestClient):
    r = client.post(
        "/api/inbound-sms/webhook?tenant=tenant-test",
        data={
            "From": "+15551234567",
            "To": "+15557654321",
            "Body": "Yes please",
            "MessageSid": "SM_abc123",
        },
    )
    assert r.status_code == 200, r.text

    rows = client.get("/api/inbound-sms").json()
    assert len(rows) == 1
    assert rows[0]["from_number"] == "+15551234567"
    assert rows[0]["body"] == "Yes please"
    assert rows[0]["provider"] == "twilio"
    assert rows[0]["provider_message_id"] == "SM_abc123"
    assert rows[0]["company_id"] == "tenant-test"


def test_sms_webhook_requires_tenant_param(client: TestClient):
    r = client.post(
        "/api/inbound-sms/webhook",
        data={
            "From": "+15551234567",
            "To": "+15557654321",
            "Body": "Hello",
        },
    )
    assert r.status_code == 400


def test_email_webhook_creates_row(client: TestClient):
    r = client.post(
        "/api/inbound-email/webhook?tenant=tenant-test",
        json={
            "from_email": "customer@example.com",
            "from_name": "John Customer",
            "to_email": "support@dealer.com",
            "subject": "Re: Your estimate",
            "body_text": "Looks good, please proceed.",
            "message_id": "<abc@mail.example.com>",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert data["id"]

    rows = client.get("/api/inbound-email").json()
    assert len(rows) == 1
    assert rows[0]["from_email"] == "customer@example.com"
    assert rows[0]["subject"] == "Re: Your estimate"


def test_public_endpoints_no_auth():
    """Hit webhook with no authed user — should still succeed (public)."""
    tc = _make_client()
    # Remove the auth override to simulate absent credentials. The public
    # router doesn't depend on get_current_user so it should still work.
    tc.app.dependency_overrides.pop(get_current_user, None)
    try:
        r1 = tc.post(
            "/api/inbound-sms/webhook?tenant=tenant-test",
            data={"From": "+1", "To": "+2", "Body": "hi"},
        )
        assert r1.status_code == 200, r1.text

        r2 = tc.post(
            "/api/inbound-email/webhook?tenant=tenant-test",
            json={
                "from_email": "a@b.com",
                "to_email": "c@d.com",
            },
        )
        assert r2.status_code == 200, r2.text
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Admin — list/retrieve with tenant scoping
# ---------------------------------------------------------------------------


def test_admin_list_sms_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        c1.post(
            "/api/inbound-sms/webhook?tenant=tenant-a",
            data={"From": "+1A", "To": "+2A", "Body": "A"},
        )
        c2.post(
            "/api/inbound-sms/webhook?tenant=tenant-b",
            data={"From": "+1B", "To": "+2B", "Body": "B"},
        )

        list_a = c1.get("/api/inbound-sms").json()
        list_b = c2.get("/api/inbound-sms").json()
        assert len(list_a) == 1 and list_a[0]["body"] == "A"
        assert len(list_b) == 1 and list_b[0]["body"] == "B"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_admin_list_email_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a", user_sub="ua")
    c2 = _make_client(tenant_id="tenant-b", user_sub="ub")
    try:
        c1.post(
            "/api/inbound-email/webhook?tenant=tenant-a",
            json={"from_email": "a@x.com", "to_email": "t@y.com", "subject": "A"},
        )
        c2.post(
            "/api/inbound-email/webhook?tenant=tenant-b",
            json={"from_email": "b@x.com", "to_email": "t@y.com", "subject": "B"},
        )

        list_a = c1.get("/api/inbound-email").json()
        list_b = c2.get("/api/inbound-email").json()
        assert len(list_a) == 1 and list_a[0]["subject"] == "A"
        assert len(list_b) == 1 and list_b[0]["subject"] == "B"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_mark_email_read(client: TestClient):
    created = client.post(
        "/api/inbound-email/webhook?tenant=tenant-test",
        json={"from_email": "a@b.com", "to_email": "t@d.com", "subject": "Read me"},
    ).json()
    email_id = created["id"]

    # Before marking — unread_only should include it
    unread = client.get("/api/inbound-email?unread_only=true").json()
    assert any(e["id"] == email_id for e in unread)

    r = client.patch(f"/api/inbound-email/{email_id}/read")
    assert r.status_code == 200, r.text
    assert r.json()["read_at"] is not None

    # After — unread_only should NOT include it
    unread2 = client.get("/api/inbound-email?unread_only=true").json()
    assert all(e["id"] != email_id for e in unread2)


def test_link_sms_to_customer(client: TestClient):
    client.post(
        "/api/inbound-sms/webhook?tenant=tenant-test",
        data={"From": "+1", "To": "+2", "Body": "Link me"},
    )
    sms_id = client.get("/api/inbound-sms").json()[0]["id"]

    customer_uuid = str(uuid4())
    r = client.post(
        f"/api/inbound-sms/{sms_id}/link",
        json={"customer_id": customer_uuid},
    )
    assert r.status_code == 200, r.text
    assert r.json()["customer_id"] == customer_uuid
    assert r.json()["processed_at"] is not None


def test_link_email_to_job(client: TestClient):
    created = client.post(
        "/api/inbound-email/webhook?tenant=tenant-test",
        json={"from_email": "a@b.com", "to_email": "t@d.com", "subject": "Job link"},
    ).json()
    email_id = created["id"]

    job_uuid = str(uuid4())
    r = client.post(
        f"/api/inbound-email/{email_id}/link",
        json={"job_id": job_uuid},
    )
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == job_uuid
