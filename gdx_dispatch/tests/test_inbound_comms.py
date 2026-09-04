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
from gdx_dispatch.core.tenant import get_company_id
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
    # The webhooks take the company from the server (get_company_id), never
    # from the request. This is the seam core/tenant.py documents for tests.
    app.dependency_overrides[get_company_id] = lambda: tenant_id
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
        "/api/inbound-sms/webhook",
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


def test_sms_webhook_needs_no_tenant_param(client: TestClient):
    """The company comes from the server, so no query param is required."""
    r = client.post(
        "/api/inbound-sms/webhook",
        data={
            "From": "+15551234567",
            "To": "+15557654321",
            "Body": "Hello",
        },
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/inbound-sms").json()[0]["company_id"] == "tenant-test"


def test_webhook_query_param_cannot_choose_the_company(client: TestClient):
    """A caller-supplied ?tenant= must not land in company_id.

    This is the defect: both webhooks stamped company_id straight from the
    query string, so anyone reaching the URL chose which company owned the
    row they were creating.
    """
    client.post(
        "/api/inbound-sms/webhook?tenant=attacker-chosen",
        data={"From": "+1", "To": "+2", "Body": "hi"},
    )
    client.post(
        "/api/inbound-email/webhook?tenant=attacker-chosen",
        json={"from_email": "a@b.com", "to_email": "c@d.com"},
    )
    sms = client.get("/api/inbound-sms").json()
    email = client.get("/api/inbound-email").json()
    assert [r["company_id"] for r in sms] == ["tenant-test"]
    assert len(email) == 1
    assert "attacker-chosen" not in str(sms) + str(email)


def test_email_webhook_creates_row(client: TestClient):
    r = client.post(
        "/api/inbound-email/webhook",
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


def test_public_endpoints_no_logged_in_user():
    """Webhooks need no *logged-in user* — they authenticate the caller instead.

    Outside a production env the Twilio signature and the email shared secret
    are both no-ops (see core/twilio_signature.py), which is what lets this
    test post without either.
    """
    tc = _make_client()
    # Remove the auth override to simulate absent credentials. The public
    # router doesn't depend on get_current_user so it should still work.
    tc.app.dependency_overrides.pop(get_current_user, None)
    try:
        r1 = tc.post(
            "/api/inbound-sms/webhook",
            data={"From": "+1", "To": "+2", "Body": "hi"},
        )
        assert r1.status_code == 200, r1.text

        r2 = tc.post(
            "/api/inbound-email/webhook",
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
            "/api/inbound-sms/webhook",
            data={"From": "+1A", "To": "+2A", "Body": "A"},
        )
        c2.post(
            "/api/inbound-sms/webhook",
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
            "/api/inbound-email/webhook",
            json={"from_email": "a@x.com", "to_email": "t@y.com", "subject": "A"},
        )
        c2.post(
            "/api/inbound-email/webhook",
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
        "/api/inbound-email/webhook",
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
        "/api/inbound-sms/webhook",
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
        "/api/inbound-email/webhook",
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


# ---------------------------------------------------------------------------
# Inbound-email shared secret (S21)
#
# The email webhook shipped with no authentication of any kind: a well-formed
# POST wrote a row into the staff inbox. Confirmed unauthenticated on prod
# 2026-09-04 (an empty body reached pydantic validation, 422). These pin the
# production posture, which is the only place the gate is enforced.
# ---------------------------------------------------------------------------


def test_email_webhook_rejected_in_prod_without_secret(client: TestClient, monkeypatch):
    """Prod + no INBOUND_EMAIL_WEBHOOK_SECRET configured => 403, no row."""
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.delenv("INBOUND_EMAIL_WEBHOOK_SECRET", raising=False)
    r = client.post(
        "/api/inbound-email/webhook",
        json={"from_email": "spoof@evil.test", "to_email": "staff@example.com"},
    )
    assert r.status_code == 403
    assert client.get("/api/inbound-email").json() == []


def test_email_webhook_rejected_in_prod_with_wrong_secret(client: TestClient, monkeypatch):
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv("INBOUND_EMAIL_WEBHOOK_SECRET", "right")
    r = client.post(
        "/api/inbound-email/webhook",
        headers={"X-GDX-Webhook-Secret": "wrong"},
        json={"from_email": "spoof@evil.test", "to_email": "staff@example.com"},
    )
    assert r.status_code == 403
    assert client.get("/api/inbound-email").json() == []


def test_email_webhook_accepted_in_prod_with_right_secret(client: TestClient, monkeypatch):
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv("INBOUND_EMAIL_WEBHOOK_SECRET", "right")
    r = client.post(
        "/api/inbound-email/webhook",
        headers={"X-GDX-Webhook-Secret": "right"},
        json={"from_email": "real@customer.test", "to_email": "staff@example.com"},
    )
    assert r.status_code == 200, r.text
    assert len(client.get("/api/inbound-email").json()) == 1


def test_email_secret_non_ascii_header_is_403_not_500(monkeypatch):
    """A non-ASCII header must not crash the gate (compare_digest TypeError).

    httpx refuses to *send* a non-ASCII header, so no TestClient request can
    reach this. The dependency is driven directly — the only way in.
    """
    import asyncio

    from fastapi import HTTPException

    from gdx_dispatch.core.inbound_email_auth import verify_inbound_email_secret

    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv("INBOUND_EMAIL_WEBHOOK_SECRET", "right")

    class _Req:
        headers = {"X-GDX-Webhook-Secret": "caf\xe9"}  # latin-1 'caf\xe9'

    with pytest.raises(HTTPException) as exc:
        asyncio.run(verify_inbound_email_secret(_Req()))
    assert exc.value.status_code == 403


def test_email_webhook_enforced_under_unrecognised_env(client: TestClient, monkeypatch):
    """An env name nobody listed must still enforce.

    core/twilio_signature.py checks membership in ("production","prod",
    "staging"), so GDX_ENV=prod-eu silently turns that gate off. This gate
    inverts the test — off only for known dev/test names — so an unrecognised
    value enforces instead of failing open.
    """
    monkeypatch.setenv("GDX_ENV", "prod-eu")
    monkeypatch.delenv("INBOUND_EMAIL_WEBHOOK_SECRET", raising=False)
    r = client.post(
        "/api/inbound-email/webhook",
        json={"from_email": "spoof@evil.test", "to_email": "staff@example.com"},
    )
    assert r.status_code == 403
    assert client.get("/api/inbound-email").json() == []


def test_email_webhook_enforced_in_dev_once_a_secret_is_set(client: TestClient, monkeypatch):
    """Configuring a secret is an explicit request to check it, in any env."""
    monkeypatch.setenv("GDX_ENV", "dev")
    monkeypatch.setenv("INBOUND_EMAIL_WEBHOOK_SECRET", "right")
    assert client.post(
        "/api/inbound-email/webhook",
        json={"from_email": "a@b.test", "to_email": "c@d.test"},
    ).status_code == 403
    assert client.post(
        "/api/inbound-email/webhook",
        headers={"X-GDX-Webhook-Secret": "right"},
        json={"from_email": "a@b.test", "to_email": "c@d.test"},
    ).status_code == 200
