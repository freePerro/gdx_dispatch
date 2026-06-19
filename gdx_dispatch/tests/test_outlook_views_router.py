"""Phase 5 / Outlook read-view router — verify endpoint shapes + visibility."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook.models import OutlookMessage
from gdx_dispatch.modules.outlook.views_router import (
    get_db_for_views,
    get_user_for_views,
    router as views_router,
)
from gdx_dispatch.routers.auth import get_current_user


UID, TID = uuid4(), uuid4()


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "admin"}


def _msg(**overrides):
    m = OutlookMessage()
    m.id = overrides.get("id", uuid4())
    m.account_id = overrides.get("account_id", uuid4())
    m.subject = overrides.get("subject", "Re: estimate")
    m.from_address = overrides.get("from_address", "alice@x.com")
    m.to_addresses = overrides.get("to_addresses", ["doug@gdx"])
    m.cc_addresses = overrides.get("cc_addresses", [])
    m.bcc_addresses = overrides.get("bcc_addresses", [])
    m.direction = overrides.get("direction", "inbound")
    m.sent_at = overrides.get("sent_at", datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc))
    m.received_at = overrides.get("received_at", datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc))
    m.body_preview = overrides.get("body_preview", "preview text")
    m.is_read = overrides.get("is_read", False)
    m.has_attachments = overrides.get("has_attachments", False)
    m.linked_customer_id = overrides.get("linked_customer_id")
    m.linked_job_id = overrides.get("linked_job_id")
    m.tag_strategy = overrides.get("tag_strategy")
    m.tag_confidence = overrides.get("tag_confidence")
    m.is_personal = overrides.get("is_personal", False)
    m.conversation_id = overrides.get("conversation_id", "conv-123")
    m.internet_message_id = overrides.get("internet_message_id", "<x@y>")
    m.body_r2_key = overrides.get("body_r2_key", "outlook/x.html")
    return m


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()
    app.include_router(views_router)
    tdb = MagicMock()
    app.dependency_overrides[get_user_for_views] = _user
    app.dependency_overrides[get_db_for_views] = lambda: tdb
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: tdb
    app.dependency_overrides[require_module("email")] = lambda: None
    return TestClient(app), tdb


# ── unified inbox ───────────────────────────────────────────────────────


def test_list_messages_returns_visible_only(app):
    client, tdb = app
    msgs = [_msg(linked_customer_id=uuid4()), _msg(linked_customer_id=uuid4())]
    tdb.query.return_value.order_by.return_value.limit.return_value.offset.return_value.all.return_value = msgs
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=msgs):
        r = client.get("/api/outlook/messages")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["subject"] == "Re: estimate"
    assert body[0]["from_address"] == "alice@x.com"


def test_list_messages_paginates(app):
    client, tdb = app
    tdb.query.return_value.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=[]):
        r = client.get("/api/outlook/messages?limit=10&offset=20")
    assert r.status_code == 200


# ── by-customer ─────────────────────────────────────────────────────────


def test_list_by_customer_filters_by_linked_id(app):
    client, tdb = app
    cid = uuid4()
    msg = _msg(linked_customer_id=cid)
    tdb.query.return_value.filter.return_value.order_by.return_value.all.return_value = [msg]
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=[msg]):
        r = client.get(f"/api/outlook/messages/by-customer/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["linked_customer_id"] == str(cid)


# ── by-job ──────────────────────────────────────────────────────────────


def test_list_by_job_filters_by_linked_id(app):
    client, tdb = app
    jid = uuid4()
    msg = _msg(linked_job_id=jid)
    tdb.query.return_value.filter.return_value.order_by.return_value.all.return_value = [msg]
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=[msg]):
        r = client.get(f"/api/outlook/messages/by-job/{jid}")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["linked_job_id"] == str(jid)


# ── detail ──────────────────────────────────────────────────────────────


def test_get_message_detail_404_when_missing(app):
    client, tdb = app
    tdb.get.return_value = None
    r = client.get(f"/api/outlook/messages/{uuid4()}")
    assert r.status_code == 404


def test_get_message_detail_404_when_not_visible(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.get(f"/api/outlook/messages/{msg.id}")
    # NEVER 403 — return 404 to avoid confirming existence.
    assert r.status_code == 404


def test_get_message_detail_returns_full_shape_when_visible(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.get(f"/api/outlook/messages/{msg.id}")
    assert r.status_code == 200
    body = r.json()
    # Detail response includes extra fields not in list shape
    assert "conversation_id" in body
    assert "internet_message_id" in body
    assert "cc_addresses" in body
