"""Tests for gdx_dispatch/modules/notifications — NotificationPreference, NotificationLog,
DeviceToken models and the notifications API router.

Uses an isolated SQLite in-memory database so no external services are required.
The JWT auth dependency is overridden with a fake user dict.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.notifications.models import DeviceToken, NotificationLog, NotificationPreference
from gdx_dispatch.modules.notifications.router import router
from gdx_dispatch.routers.auth import get_current_user

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-test-001"
USER_ID = "user-test-001"
OTHER_USER_ID = "user-test-999"

FAKE_USER = {"user_id": USER_ID, "tenant_id": TENANT_ID, "role": "admin"}


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = _make_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(db_session):
    """FastAPI TestClient with DB and auth overrides."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _make_notif(db, user_id=USER_ID, tenant_id=TENANT_ID, read=False, status="sent"):
    n = NotificationLog(
        tenant_id=tenant_id,
        user_id=user_id,
        notification_type="job_update",
        channel="in_app",
        subject="Test notification",
        body="A job was updated.",
        status=status,
        sent_at=utcnow(),
        read_at=utcnow() if read else None,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


# ---------------------------------------------------------------------------
# Test 1: GET /notifications — returns only unread for current user
# ---------------------------------------------------------------------------


def test_list_notifications_returns_unread_only(client, db_session):
    """Only unread notifications belonging to current user are returned."""
    _make_notif(db_session, read=False)
    _make_notif(db_session, read=True)  # already read — should be excluded
    _make_notif(db_session, user_id=OTHER_USER_ID, read=False)  # wrong user

    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["user_id"] == USER_ID
    assert data[0]["read_at"] is None


# ---------------------------------------------------------------------------
# Test 2: GET /notifications/unread-count
# ---------------------------------------------------------------------------


def test_unread_count(client, db_session):
    """Badge count matches number of unread notifications."""
    _make_notif(db_session, read=False)
    _make_notif(db_session, read=False)
    _make_notif(db_session, read=True)

    resp = client.get("/api/notifications/unread-count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


# ---------------------------------------------------------------------------
# Test 3: POST /notifications/{id}/read — marks single notification read
# ---------------------------------------------------------------------------


def test_mark_single_notification_read(client, db_session):
    """Marking a single notification sets its read_at timestamp."""
    notif = _make_notif(db_session, read=False)

    resp = client.post(f"/api/notifications/{notif.id}/read")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    db_session.refresh(notif)
    assert notif.read_at is not None


# ---------------------------------------------------------------------------
# Test 4: POST /notifications/{id}/read — 404 for wrong user's notification
# ---------------------------------------------------------------------------


def test_mark_read_wrong_user_returns_404(client, db_session):
    """Cannot mark another user's notification as read — 404 expected."""
    other_notif = _make_notif(db_session, user_id=OTHER_USER_ID, read=False)

    resp = client.post(f"/api/notifications/{other_notif.id}/read")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 5: POST /notifications/read-all — marks all unread read
# ---------------------------------------------------------------------------


def test_mark_all_read(client, db_session):
    """Mark-all-read sets read_at on every unread notification for current user."""
    n1 = _make_notif(db_session, read=False)
    n2 = _make_notif(db_session, read=False)
    _make_notif(db_session, user_id=OTHER_USER_ID, read=False)  # should not be affected

    resp = client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json()["marked"] == 2

    db_session.refresh(n1)
    db_session.refresh(n2)
    assert n1.read_at is not None
    assert n2.read_at is not None


# ---------------------------------------------------------------------------
# Test 6: PATCH /notifications/preferences — create and update preference
# ---------------------------------------------------------------------------


def test_upsert_preference_create_then_update(client, db_session):
    """Preference is created on first PATCH; subsequent PATCH updates it."""
    payload = {"notification_type": "job_update", "channel": "email", "is_enabled": True}

    # Create
    resp = client.patch("/api/notifications/preferences", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_enabled"] is True
    pref_id = data["id"]

    # Update — disable
    payload["is_enabled"] = False
    resp2 = client.patch("/api/notifications/preferences", json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["is_enabled"] is False
    # Same row updated — same id
    assert data2["id"] == pref_id


# ---------------------------------------------------------------------------
# Test 7: GET /notifications/preferences — lists user's preferences
# ---------------------------------------------------------------------------


def test_get_preferences(client, db_session):
    """GET /preferences returns all preferences for the current user."""
    for ch in ("email", "sms"):
        p = NotificationPreference(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            notification_type="payment_received",
            channel=ch,
            is_enabled=True,
        )
        db_session.add(p)
    # Another user's pref — should NOT appear
    other = NotificationPreference(
        tenant_id=TENANT_ID,
        user_id=OTHER_USER_ID,
        notification_type="payment_received",
        channel="email",
        is_enabled=False,
    )
    db_session.add(other)
    db_session.commit()

    resp = client.get("/api/notifications/preferences")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(p["user_id"] == USER_ID for p in data)


# ---------------------------------------------------------------------------
# Test 8: POST /devices/register and DELETE /devices/{token}
# ---------------------------------------------------------------------------


def test_device_register_and_deregister(client, db_session):
    """Register a device token, then deregister it; is_active reflects state."""
    token_val = f"device-token-{uuid.uuid4()}"

    # Register
    resp = client.post(
        "/api/devices/register",
        json={"platform": "web", "token": token_val},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_active"] is True
    assert data["platform"] == "web"
    assert data["token"] == token_val

    # Re-registering the same token should return the existing row active
    resp2 = client.post(
        "/api/devices/register",
        json={"platform": "web", "token": token_val},
    )
    assert resp2.status_code == 200
    assert resp2.json()["id"] == data["id"]

    # Deregister
    resp3 = client.delete(f"/api/devices/{token_val}")
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "deregistered"

    # Verify is_active is False in DB
    device = db_session.query(DeviceToken).filter_by(token=token_val).one()
    assert device.is_active is False


# ---------------------------------------------------------------------------
# Test 9 (bonus): DELETE /devices/{token} — 404 for unknown token
# ---------------------------------------------------------------------------


def test_deregister_unknown_device_returns_404(client):
    """Deleting a non-existent device token returns 404."""
    resp = client.delete("/api/devices/no-such-token-xyz")
    assert resp.status_code == 404
