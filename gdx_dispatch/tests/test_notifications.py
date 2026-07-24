"""Coverage for the notifications surfaces left untested after the A3 cleanup.

Two independent things share the "notifications" name and neither had tests
after ``modules/notifications/router.py`` was deleted (CLEANUP_BACKLOG A3):

1. ``modules/notifications/models.py`` — three ``TenantBase`` ORM models
   (``NotificationPreference``, ``NotificationLog``, ``DeviceToken``) that are
   still registered in ``models/__init__.py`` but are NOT used by any live
   router. Covered here with direct-ORM CRUD / default / tenant-isolation /
   unique-constraint tests, mirroring ``test_22_locations.py``.

2. ``routers/notifications.py`` — the LIVE, mounted router exposing settings /
   templates / send / history plus the in-app badge endpoints (count / list /
   mark-read). It is gated by ``require_module("communications")`` and
   ``get_current_user``. Covered here with functional ``TestClient`` tests,
   mirroring ``test_appointments.py`` / ``test_communications.py``.

These are two different storage backings: the router persists to the
``tenant_models`` tables (``notifications``, ``notification_templates``, …),
NOT to the ``modules/notifications`` tables. Both happen to live on
``TenantBase.metadata``, so one ``create_all`` covers everything.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register router models on TenantBase.metadata)
from gdx_dispatch.modules.notifications.models import (
    DeviceToken,
    NotificationLog,
    NotificationPreference,
)
from gdx_dispatch.routers.auth import get_current_user

# ===========================================================================
# Part 1 — modules/notifications/models.py  (direct-ORM model tests)
# ===========================================================================


@pytest.fixture
def notif_db():
    """Isolated in-memory SQLite DB with the notifications module tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


def test_create_notification_preference(notif_db):
    """A NotificationPreference persists with its is_enabled default and timestamps."""
    pref = NotificationPreference(
        tenant_id="tenant-abc",
        user_id="user-1",
        notification_type="appointment_reminder",
        channel="sms",
    )
    notif_db.add(pref)
    notif_db.commit()
    notif_db.refresh(pref)

    assert pref.id is not None
    assert pref.tenant_id == "tenant-abc"
    assert pref.notification_type == "appointment_reminder"
    assert pref.channel == "sms"
    assert pref.is_enabled is True  # column default
    assert pref.created_at is not None
    assert pref.updated_at is not None


def test_notification_preference_opt_out(notif_db):
    """is_enabled can be set False to record an opt-out."""
    pref = NotificationPreference(
        tenant_id="tenant-abc",
        user_id="user-1",
        notification_type="payment_received",
        channel="email",
        is_enabled=False,
    )
    notif_db.add(pref)
    notif_db.commit()
    notif_db.refresh(pref)
    assert pref.is_enabled is False


def test_notification_preference_tenant_isolation(notif_db):
    """Preferences are scoped per tenant_id."""
    notif_db.add_all([
        NotificationPreference(
            tenant_id="tenant-111", user_id="u", notification_type="job_update", channel="push"
        ),
        NotificationPreference(
            tenant_id="tenant-222", user_id="u", notification_type="job_update", channel="push"
        ),
    ])
    notif_db.commit()

    rows = notif_db.execute(
        select(NotificationPreference).where(NotificationPreference.tenant_id == "tenant-111")
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].tenant_id == "tenant-111"


def test_create_notification_log_defaults_to_pending(notif_db):
    """A NotificationLog defaults status to 'pending' before delivery."""
    entry = NotificationLog(
        tenant_id="tenant-abc",
        user_id="user-1",
        notification_type="new_message",
        channel="in_app",
        subject="New message",
        body="You have a new message",
    )
    notif_db.add(entry)
    notif_db.commit()
    notif_db.refresh(entry)

    assert entry.status == "pending"
    assert entry.sent_at is None
    assert entry.read_at is None
    assert entry.created_at is not None


def test_notification_log_marks_sent(notif_db):
    """A NotificationLog can transition to 'sent' with a sent_at timestamp."""
    from gdx_dispatch.core.audit import utcnow

    entry = NotificationLog(
        tenant_id="tenant-abc",
        user_id="user-1",
        notification_type="appointment_reminder",
        channel="sms",
        status="pending",
    )
    notif_db.add(entry)
    notif_db.commit()

    entry.status = "sent"
    entry.sent_at = utcnow()
    notif_db.commit()
    notif_db.refresh(entry)

    assert entry.status == "sent"
    assert entry.sent_at is not None


def test_create_device_token(notif_db):
    """A DeviceToken persists with platform and the is_active default."""
    tok = DeviceToken(
        tenant_id="tenant-abc",
        user_id="user-1",
        platform="ios",
        token="apns-token-001",
    )
    notif_db.add(tok)
    notif_db.commit()
    notif_db.refresh(tok)

    assert tok.id is not None
    assert tok.platform == "ios"
    assert tok.is_active is True  # column default


def test_device_token_unique_constraint(notif_db):
    """The token column is unique — a duplicate token raises IntegrityError."""
    notif_db.add(DeviceToken(tenant_id="t1", user_id="u1", platform="web", token="dup-token"))
    notif_db.commit()

    notif_db.add(DeviceToken(tenant_id="t2", user_id="u2", platform="android", token="dup-token"))
    with pytest.raises(IntegrityError):
        notif_db.commit()
    notif_db.rollback()


def test_device_token_tenant_isolation(notif_db):
    """Device tokens are scoped per tenant_id."""
    notif_db.add_all([
        DeviceToken(tenant_id="tenant-111", user_id="u", platform="ios", token="tok-a"),
        DeviceToken(tenant_id="tenant-222", user_id="u", platform="ios", token="tok-b"),
    ])
    notif_db.commit()

    rows = notif_db.execute(
        select(DeviceToken).where(DeviceToken.tenant_id == "tenant-111")
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].token == "tok-a"


# ===========================================================================
# Part 2 — routers/notifications.py  (functional TestClient tests)
# ===========================================================================

TENANT_ID = "tenant-test"


def _make_client(*, grant_module: bool = True, tenant_id: str = TENANT_ID) -> TestClient:
    """Build an isolated app mounting only the notifications router.

    Mirrors test_appointments._make_client. When ``grant_module`` is False we
    seed a *different* module (``jobs``) instead of ``communications``: that
    leaves at least one grant row, so ``_seed_default_modules`` does NOT
    auto-grant every module on first check, and ``communications`` stays absent
    — letting ``require_module`` 403. (Seeding zero rows would single-tenant
    seed everything, masking the gate.)
    """
    from gdx_dispatch.routers.notifications import router

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    seed_key = "communications" if grant_module else "jobs"
    setup = Session()
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, :mod, datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g-{seed_key}-{tenant_id}", "tid": tenant_id, "mod": seed_key},
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
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    tc._Session = Session  # type: ignore[attr-defined]
    return tc


@pytest.fixture
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# --- settings ---------------------------------------------------------------

def test_get_settings_creates_defaults(client):
    """First GET seeds and returns default settings."""
    resp = client.get("/api/notifications/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"email_enabled": True, "sms_enabled": True, "sender_name": "Dispatch Team"}


def test_patch_settings_partial_update_persists(client):
    """PATCH applies only supplied fields and persists across requests."""
    client.get("/api/notifications/settings")  # seed row
    resp = client.patch(
        "/api/notifications/settings",
        json={"sms_enabled": False, "sender_name": "Acme Garage"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["sms_enabled"] is False
    assert data["sender_name"] == "Acme Garage"
    assert data["email_enabled"] is True  # untouched

    again = client.get("/api/notifications/settings")
    assert again.json() == {
        "email_enabled": True,
        "sms_enabled": False,
        "sender_name": "Acme Garage",
    }


# --- templates --------------------------------------------------------------

def test_list_templates_seeds_defaults(client):
    """GET templates seeds the five default keys, all flagged is_default."""
    resp = client.get("/api/notifications/templates")
    assert resp.status_code == 200
    rows = resp.json()
    keys = {r["key"] for r in rows}
    assert {
        "appointment_reminder_24h",
        "on_my_way",
        "job_completed",
        "review_request",
        "payment_received",
    } <= keys
    assert all(r["is_default"] for r in rows)


def test_create_template(client):
    """POST creates a non-default template and returns it 201."""
    resp = client.post(
        "/api/notifications/templates",
        json={"key": "custom_followup", "subject": "Following up", "body": "Hi {name}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["key"] == "custom_followup"
    assert data["subject"] == "Following up"
    assert data["is_default"] is False
    assert data["id"]

    listed = client.get("/api/notifications/templates").json()
    assert any(r["key"] == "custom_followup" and not r["is_default"] for r in listed)


def test_create_template_rejects_blank_fields(client):
    """Empty key/subject/body fail validation (min_length=1)."""
    resp = client.post(
        "/api/notifications/templates",
        json={"key": "", "subject": "", "body": ""},
    )
    assert resp.status_code == 422


# --- send -------------------------------------------------------------------

def test_send_uses_default_template_body(client):
    """Sending against a seeded default renders that template's body."""
    resp = client.post(
        "/api/notifications/send",
        json={"customer_id": "cust-1", "template_key": "on_my_way", "channel": "sms"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "sent"
    assert data["customer_id"] == "cust-1"
    assert data["channel"] == "sms"
    assert data["rendered_message"] == "Default template for on_my_way"


def test_send_manual_message_overrides_template(client):
    """A manual_message overrides the template body in the rendered output."""
    resp = client.post(
        "/api/notifications/send",
        json={
            "customer_id": "cust-1",
            "template_key": "on_my_way",
            "channel": "email",
            "manual_message": "Custom note",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["rendered_message"] == "Custom note"


def test_send_unknown_template_404(client):
    """Sending against a non-existent template returns 404."""
    resp = client.post(
        "/api/notifications/send",
        json={"customer_id": "cust-1", "template_key": "does_not_exist", "channel": "sms"},
    )
    assert resp.status_code == 404


def test_send_rejects_invalid_channel(client):
    """channel is constrained to sms|email by the request schema."""
    resp = client.post(
        "/api/notifications/send",
        json={"customer_id": "cust-1", "template_key": "on_my_way", "channel": "carrier_pigeon"},
    )
    assert resp.status_code == 422


# --- history ----------------------------------------------------------------

def test_history_lists_sent_and_paginates(client):
    """History returns sent records with an accurate total and respects page_size."""
    for i in range(3):
        client.post(
            "/api/notifications/send",
            json={"customer_id": f"cust-{i}", "template_key": "on_my_way", "channel": "sms"},
        )

    resp = client.get("/api/notifications/history", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["template_key"] == "on_my_way"


# --- in-app badge endpoints (count / list / mark-read) ----------------------

def _seed_notification(tc: TestClient, *, notif_id: str, user_id: str | None, is_read: int = 0) -> None:
    """Insert an in-app Notification row directly via the shared engine."""
    db = tc._Session()  # type: ignore[attr-defined]
    try:
        db.execute(
            text(
                """
                INSERT INTO notifications
                    (id, tenant_id, user_id, title, message, category, is_read, created_at)
                VALUES (:id, :tid, :uid, :title, :msg, 'system', :is_read, datetime('now'))
                """
            ),
            {
                "id": notif_id,
                "tid": TENANT_ID,
                "uid": user_id,
                "title": "Heads up",
                "msg": "Something happened",
                "is_read": is_read,
            },
        )
        db.commit()
    finally:
        db.close()


def test_count_unread_includes_user_and_broadcast(client):
    """Unread count covers this user's rows plus broadcast (user_id NULL) rows."""
    _seed_notification(client, notif_id="n1", user_id="user-1", is_read=0)
    _seed_notification(client, notif_id="n2", user_id=None, is_read=0)       # broadcast
    _seed_notification(client, notif_id="n3", user_id="user-1", is_read=1)   # already read
    _seed_notification(client, notif_id="n4", user_id="other-user", is_read=0)  # someone else

    resp = client.get("/api/notifications/count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


def test_list_notifications_excludes_other_users(client):
    """The list returns this user's + broadcast rows, not other users' rows."""
    _seed_notification(client, notif_id="n1", user_id="user-1")
    _seed_notification(client, notif_id="n2", user_id=None)
    _seed_notification(client, notif_id="n3", user_id="other-user")

    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    ids = {item["id"] for item in data["items"]}
    assert ids == {"n1", "n2"}


def test_mark_notification_read(client):
    """Marking a notification read flips is_read and drops it from the unread count."""
    _seed_notification(client, notif_id="n1", user_id="user-1", is_read=0)
    assert client.get("/api/notifications/count").json() == {"count": 1}

    resp = client.post("/api/notifications/n1/read")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert client.get("/api/notifications/count").json() == {"count": 0}


def test_mark_unknown_notification_404(client):
    """Marking a non-existent notification returns 404."""
    resp = client.post(f"/api/notifications/{uuid4()}/read")
    assert resp.status_code == 404


# --- delete / clear-all (2026-07-24) ----------------------------------------

def test_delete_notification_soft_deletes(client):
    """Delete drops the row from list + count; a second delete 404s."""
    _seed_notification(client, notif_id="n1", user_id="user-1", is_read=0)
    _seed_notification(client, notif_id="n2", user_id=None, is_read=0)

    resp = client.delete("/api/notifications/n1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    data = client.get("/api/notifications").json()
    assert {item["id"] for item in data["items"]} == {"n2"}
    assert client.get("/api/notifications/count").json() == {"count": 1}
    # Already soft-deleted → 404, not a double delete.
    assert client.delete("/api/notifications/n1").status_code == 404


def test_delete_broadcast_notification(client):
    """A broadcast row (user_id NULL) can be deleted by any user — one office,
    'delete' means handled for everyone."""
    _seed_notification(client, notif_id="b1", user_id=None, is_read=0)
    assert client.delete("/api/notifications/b1").status_code == 200
    assert client.get("/api/notifications/count").json() == {"count": 0}


def test_delete_other_users_notification_404(client):
    """Another user's personal notification is invisible to delete."""
    _seed_notification(client, notif_id="n3", user_id="other-user")
    assert client.delete("/api/notifications/n3").status_code == 404


def test_mark_read_deleted_notification_404(client):
    """A deleted notification can't be marked read."""
    _seed_notification(client, notif_id="n1", user_id="user-1")
    assert client.delete("/api/notifications/n1").status_code == 200
    assert client.post("/api/notifications/n1/read").status_code == 404


def test_clear_all_notifications(client):
    """Clear-all soft-deletes everything visible to the caller (own + broadcast,
    read or unread) and leaves other users' rows alone."""
    _seed_notification(client, notif_id="n1", user_id="user-1", is_read=0)
    _seed_notification(client, notif_id="n2", user_id=None, is_read=1)
    _seed_notification(client, notif_id="n3", user_id="other-user", is_read=0)

    resp = client.delete("/api/notifications")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 2}
    assert client.get("/api/notifications").json()["total"] == 0
    assert client.get("/api/notifications/count").json() == {"count": 0}

    # The other user's row must be untouched.
    db = client._Session()  # type: ignore[attr-defined]
    try:
        row = db.execute(
            text("SELECT deleted_at FROM notifications WHERE id = 'n3'")
        ).scalar()
    finally:
        db.close()
    assert row is None


# --- gating -----------------------------------------------------------------

def test_requires_communications_module():
    """When communications is not granted, require_module 403s the route."""
    tc = _make_client(grant_module=False)
    try:
        resp = tc.get("/api/notifications/settings")
        assert resp.status_code == 403
    finally:
        tc.app.dependency_overrides.clear()
        tc._engine.dispose()  # type: ignore[attr-defined]
