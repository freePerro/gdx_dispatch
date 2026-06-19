from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers import communications


class StubSMSSender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_sms(self, **kwargs) -> dict:
        msg_id = f"sms-{len(self.calls) + 1}"
        self.calls.append({"id": msg_id, **kwargs})
        return {"message_id": msg_id, "status": "queued", "sent": True, "provider": "stub"}


class StubEmailSender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_email(self, **kwargs) -> dict:
        msg_id = f"email-{len(self.calls) + 1}"
        self.calls.append({"id": msg_id, **kwargs})
        return {"message_id": msg_id, "status": "queued", "sent": True, "provider": "stub"}


def _make_app() -> tuple[FastAPI, StubSMSSender, StubEmailSender]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.database import get_db

    sms_sender = StubSMSSender()
    email_sender = StubEmailSender()

    # In-memory DB for module gating
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    # Pre-seed all modules the comm tests might need
    for mod in ("communications", "jobs", "sms", "email"):
        db.execute(text("""
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, 'test-tenant', :mod, datetime('now'), datetime('now'))
        """), {"id": f"cg-{mod}", "mod": mod})
    db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "test-tenant"}
        return await call_next(request)

    app.include_router(communications.router)
    app.include_router(communications.public_router)

    # The router now requires an authenticated user; the inbound webhook
    # (public_router) does not. Override get_current_user so the authed
    # endpoints are reachable in tests.
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "test-user",
        "role": "admin",
        "tenant_id": "test-tenant",
    }

    async def _sms_override() -> StubSMSSender:
        return sms_sender

    async def _email_override() -> StubEmailSender:
        return email_sender

    app.dependency_overrides[communications.get_sms_sender] = _sms_override
    app.dependency_overrides[communications.get_email_sender] = _email_override
    app.dependency_overrides[get_db] = lambda: db
    return app, sms_sender, email_sender


@pytest.fixture(autouse=True)
def _reset_communications_state() -> None:
    communications.reset_state()


@pytest.mark.anyio
async def test_send_sms_success_calls_injected_sender() -> None:
    app, sms_sender, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sms/send", json={"to": "+15551234567", "body": "Hello"})

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "queued"
    assert data["to"] == "+15551234567"
    assert len(sms_sender.calls) == 1
    assert sms_sender.calls[0]["body"] == "Hello"


@pytest.mark.anyio
async def test_send_sms_includes_job_id_when_provided() -> None:
    app, sms_sender, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/sms/send",
            json={"to": "+15559876543", "body": "On the way", "job_id": "job-123"},
        )

    assert resp.status_code == 201
    assert resp.json()["job_id"] == "job-123"
    # job_id is stored in the response and in-memory list, not passed to the SMS sender
    assert len(sms_sender.calls) == 1


@pytest.mark.anyio
async def test_send_sms_requires_to_and_body() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sms/send", json={"to": "", "body": ""})

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_sms_conversations_empty_when_no_messages() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/sms/conversations")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_sms_webhook_creates_inbound_message_from_form_payload() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sms/webhook", data={"From": "+15550001111", "Body": "YES"})

        assert resp.status_code == 200
        body = resp.text.strip()
        assert "<Response" in body

        thread = await client.get("/api/sms/conversations/%2B15550001111")

    assert thread.status_code == 200
    messages = thread.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["direction"] == "inbound"
    assert messages[0]["body"] == "YES"


@pytest.mark.anyio
async def test_sms_webhook_accepts_json_payload() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sms/webhook", json={"from": "+15550002222", "body": "Need update"})
        assert resp.status_code == 200

        thread = await client.get("/api/sms/conversations/%2B15550002222")

    assert thread.status_code == 200
    assert thread.json()["messages"][0]["body"] == "Need update"


@pytest.mark.anyio
async def test_sms_conversations_grouped_by_phone_with_latest_message() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/sms/send", json={"to": "+15550003333", "body": "Outbound first"})
        await client.post("/api/sms/webhook", data={"From": "+15550003333", "Body": "Inbound latest"})

        resp = await client.get("/api/sms/conversations")

    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["phone"] == "+15550003333"
    assert rows[0]["count"] == 2
    assert rows[0]["last_body"] == "Inbound latest"
    assert rows[0]["direction"] == "inbound"


@pytest.mark.anyio
async def test_sms_conversation_thread_returns_chronological_messages() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/sms/webhook", data={"From": "+15550004444", "Body": "First"})
        await client.post("/api/sms/send", json={"to": "+15550004444", "body": "Second"})

        resp = await client.get("/api/sms/conversations/%2B15550004444")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["phone"] == "+15550004444"
    assert [m["direction"] for m in payload["messages"]] == ["inbound", "outbound"]


@pytest.mark.anyio
async def test_sms_conversation_unknown_phone_returns_empty_thread() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/sms/conversations/%2B19999999999")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["phone"] == "+19999999999"
    assert payload["messages"] == []


@pytest.mark.anyio
async def test_inbox_unread_count_tracks_inbound_messages() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/sms/send", json={"to": "+15550005555", "body": "Outbound"})
        await client.post("/api/sms/webhook", data={"From": "+15550005555", "Body": "Inbound one"})
        await client.post("/api/sms/webhook", data={"From": "+15550006666", "Body": "Inbound two"})

        resp = await client.get("/api/inbox/unread-count")

    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


@pytest.mark.anyio
async def test_inbox_folders_returns_sms_and_email_buckets() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/sms/webhook", data={"From": "+15550007777", "Body": "Unread SMS"})

        resp = await client.get("/api/inbox/folders")

    assert resp.status_code == 200
    folders = resp.json()["folders"]
    names = {f["name"] for f in folders}
    assert "SMS" in names
    assert "Email" in names
    sms_folder = next(f for f in folders if f["name"] == "SMS")
    assert sms_folder["unread"] == 1


@pytest.mark.anyio
async def test_send_email_success_calls_injected_sender() -> None:
    app, _, email_sender = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/email/send",
            json={"to": "user@example.com", "subject": "Hello", "body": "Email body"},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "queued"
    assert data["to"] == "user@example.com"
    assert len(email_sender.calls) == 1


@pytest.mark.anyio
async def test_send_email_requires_to_subject_and_body() -> None:
    app, _, _ = _make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/email/send", json={"to": "", "subject": "", "body": ""})

    assert resp.status_code == 422


def test_create_app_registers_communications_routes() -> None:
    app_source = open("gdx_dispatch/app.py", encoding="utf-8").read()

    assert "from gdx_dispatch.routers import communications as communications_router" in app_source
    assert "app.include_router(" in app_source
    assert "communications_router.router if hasattr(communications_router, \"router\") else communications_router" in app_source
