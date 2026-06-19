"""Tests for the team messages router."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.messages import router


def _make_client(
    tenant_id: str = "tenant-test",
    user_id: str = "alice",
    role: str = "admin",
) -> TestClient:
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
            CREATE TABLE IF NOT EXISTS tenant_module_grants (
                id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT
            )
            """
        )
    )
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO tenant_module_grants
              (id, tenant_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g1-{tenant_id}", "tid": tenant_id},
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants
              (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
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

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": user_id,
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "name": user_id,
        "role": role,
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


def _switch_user(tc: TestClient, user_id: str, role: str = "tech") -> None:
    tc.app.dependency_overrides[get_current_user] = lambda: {
        "sub": user_id,
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "name": user_id,
        "role": role,
        "tenant_id": "tenant-test",
    }


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def test_send_message_creates_recipients(client: TestClient):
    r = client.post(
        "/api/messages",
        json={
            "subject": "Hello team",
            "body": "Morning standup at 9",
            "recipient_ids": ["bob", "carol"],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["sender_id"] == "alice"
    assert len(data["recipients"]) == 2
    rids = {r["recipient_id"] for r in data["recipients"]}
    assert rids == {"bob", "carol"}


def test_inbox_returns_only_current_user_messages(client: TestClient):
    # alice sends to bob
    client.post(
        "/api/messages",
        json={"subject": "to bob", "body": "hi bob", "recipient_ids": ["bob"]},
    )
    # alice sends to carol
    client.post(
        "/api/messages",
        json={"subject": "to carol", "body": "hi carol", "recipient_ids": ["carol"]},
    )
    # alice's own inbox = empty (she sent, not received)
    r = client.get("/api/messages")
    assert r.status_code == 200
    assert r.json() == []

    _switch_user(client, "bob")
    r = client.get("/api/messages")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["subject"] == "to bob"


def test_unread_count(client: TestClient):
    client.post(
        "/api/messages",
        json={"body": "m1", "recipient_ids": ["bob"]},
    )
    client.post(
        "/api/messages",
        json={"body": "m2", "recipient_ids": ["bob", "carol"]},
    )
    _switch_user(client, "bob")
    r = client.get("/api/messages/unread_count")
    assert r.status_code == 200
    assert r.json() == {"count": 2}

    _switch_user(client, "carol")
    r = client.get("/api/messages/unread_count")
    assert r.json() == {"count": 1}


def test_mark_read_sets_timestamp(client: TestClient):
    sent = client.post(
        "/api/messages",
        json={"body": "ping", "recipient_ids": ["bob"]},
    ).json()
    mid = sent["id"]

    _switch_user(client, "bob")
    r = client.patch(f"/api/messages/{mid}/read")
    assert r.status_code == 204

    # unread_count should now be 0
    r = client.get("/api/messages/unread_count")
    assert r.json() == {"count": 0}

    # inbox row should have read_at
    inbox = client.get("/api/messages").json()
    assert len(inbox) == 1
    assert inbox[0]["read_at"] is not None


def test_mark_all_read(client: TestClient):
    for i in range(3):
        client.post(
            "/api/messages",
            json={"body": f"m{i}", "recipient_ids": ["bob"]},
        )
    _switch_user(client, "bob")
    assert client.get("/api/messages/unread_count").json() == {"count": 3}

    r = client.post("/api/messages/mark-all-read")
    assert r.status_code == 200
    assert r.json() == {"marked": 3}
    assert client.get("/api/messages/unread_count").json() == {"count": 0}


def test_sender_can_delete_own_message(client: TestClient):
    sent = client.post(
        "/api/messages",
        json={"body": "bye", "recipient_ids": ["bob"]},
    ).json()
    r = client.delete(f"/api/messages/{sent['id']}")
    assert r.status_code == 204

    # bob should no longer see it in inbox
    _switch_user(client, "bob")
    assert client.get("/api/messages").json() == []


def test_non_sender_cannot_delete(client: TestClient):
    sent = client.post(
        "/api/messages",
        json={"body": "hush", "recipient_ids": ["bob"]},
    ).json()
    # bob is a plain tech recipient, not sender, not admin
    _switch_user(client, "bob", role="tech")
    r = client.delete(f"/api/messages/{sent['id']}")
    assert r.status_code == 403


def test_admin_can_delete_any_message(client: TestClient):
    # alice (admin) sends; bob receives; carol (another admin) deletes
    sent = client.post(
        "/api/messages",
        json={"body": "audit me", "recipient_ids": ["bob"]},
    ).json()
    _switch_user(client, "carol", role="admin")
    r = client.delete(f"/api/messages/{sent['id']}")
    assert r.status_code == 204


def test_reject_empty_recipients(client: TestClient):
    r = client.post(
        "/api/messages",
        json={"body": "nowhere", "recipient_ids": []},
    )
    assert r.status_code == 422


def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a", user_id="alice")
    c2 = _make_client(tenant_id="tenant-b", user_id="alice")
    try:
        # alice@tenant-a sends to bob
        m1 = c1.post(
            "/api/messages",
            json={"body": "A-secret", "recipient_ids": ["bob"]},
        )
        assert m1.status_code == 201
        mid = m1.json()["id"]

        # bob@tenant-b must not see it, regardless of same recipient_id
        c2.app.dependency_overrides[get_current_user] = lambda: {
            "sub": "bob",
            "user_id": "bob",
            "email": "bob@example.com",
            "role": "tech",
            "tenant_id": "tenant-b",
        }
        inbox = c2.get("/api/messages")
        assert inbox.status_code == 200
        assert inbox.json() == []

        # And mark-read for that message id in tenant-b must 404
        r = c2.patch(f"/api/messages/{mid}/read")
        assert r.status_code == 404
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]
