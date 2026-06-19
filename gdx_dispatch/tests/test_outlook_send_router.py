"""Phase 6 / Outlook send endpoint — verify Graph wire format + error handling."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.send_router import (
    _build_graph_body,
    SendMailIn,
    get_db_for_send,
    get_db_for_send,
    get_user_for_send,
    router as send_router,
)
from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired
from gdx_dispatch.routers.auth import get_current_user


UID, TID = uuid4(), uuid4()


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "technician"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()
    app.include_router(send_router)
    cdb = MagicMock()
    tdb = MagicMock()
    app.dependency_overrides[get_user_for_send] = _user
    app.dependency_overrides[get_db_for_send] = lambda: cdb
    app.dependency_overrides[get_db_for_send] = lambda: tdb
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: cdb
    app.dependency_overrides[get_db] = lambda: tdb
    app.dependency_overrides[require_module("email")] = lambda: None
    return TestClient(app)


# ── _build_graph_body ──────────────────────────────────────────────────


def test_build_graph_body_simple():
    payload = SendMailIn(
        to=["doug@gdx.com"], subject="hi", body_html="<p>hi</p>",
    )
    body = _build_graph_body(payload)
    assert body["message"]["subject"] == "hi"
    assert body["message"]["body"]["contentType"] == "html"
    assert body["message"]["toRecipients"] == [{"emailAddress": {"address": "doug@gdx.com"}}]
    assert body["saveToSentItems"] is True


def test_build_graph_body_with_cc_bcc():
    """New-mail body carries cc/bcc; reply threading is handled by routing
    to /me/messages/{id}/reply, not by injecting headers into /me/sendMail."""
    payload = SendMailIn(
        to=["a@x.com"], cc=["b@x.com"], bcc=["c@x.com"],
        subject="re", body_html="<p>x</p>",
    )
    body = _build_graph_body(payload)
    assert body["message"]["ccRecipients"] == [{"emailAddress": {"address": "b@x.com"}}]
    assert body["message"]["bccRecipients"] == [{"emailAddress": {"address": "c@x.com"}}]
    # No internetMessageHeaders — reply path doesn't pass through this builder
    assert "internetMessageHeaders" not in body["message"]


# ── endpoint ───────────────────────────────────────────────────────────


def test_send_happy_path_returns_ok(app):
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post(
            "/api/outlook/send",
            json={"to": ["doug@gdx.com"], "subject": "hi", "body_html": "<p>hi</p>"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "detail": None}
    fake_gc._request.assert_called_once()
    assert fake_gc._request.call_args.args[0] == "POST"
    assert fake_gc._request.call_args.args[1] == "/me/sendMail"


def test_send_reconnect_required_returns_409(app):
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.side_effect = OutlookReconnectRequired("not connected")
        r = app.post(
            "/api/outlook/send",
            json={"to": ["doug@gdx.com"], "subject": "hi", "body_html": "<p>hi</p>"},
        )
    assert r.status_code == 409
    assert "reconnect" in r.text.lower()


def test_send_graph_failure_returns_502(app):
    fake_gc = MagicMock()
    fake_gc._request.side_effect = OutlookGraphAPIError(403, {"error": "Forbidden"})
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post(
            "/api/outlook/send",
            json={"to": ["doug@gdx.com"], "subject": "hi", "body_html": "<p>hi</p>"},
        )
    assert r.status_code == 502
    assert "403" in r.text


def test_send_validation_rejects_empty_to(app):
    r = app.post(
        "/api/outlook/send",
        json={"to": [], "subject": "hi", "body_html": "<p>hi</p>"},
    )
    assert r.status_code == 422


def test_send_reply_routes_to_messages_reply_endpoint(app):
    """When in_reply_to resolves to an OutlookMessage with a graph_message_id,
    Graph receives POST /me/messages/{graph_id}/reply, not /me/sendMail.
    Graph itself wires In-Reply-To + References headers."""
    parent_uuid = uuid4()
    fake_parent = MagicMock()
    fake_parent.graph_message_id = "AAMkAGI=GRAPH-ID"
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"):
        ctx.return_value.__enter__.return_value = fake_gc
        # tenant_db.query(OutlookMessage).filter(...).one_or_none() → parent
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = fake_parent
        r = app.post(
            "/api/outlook/send",
            json={
                "to": ["doug@gdx.com"], "subject": "Re: hi",
                "body_html": "<p>thanks</p>", "in_reply_to": str(parent_uuid),
            },
        )
    assert r.status_code == 200
    fake_gc._request.assert_called_once()
    method, path = fake_gc._request.call_args.args[0:2]
    assert method == "POST"
    assert path == "/me/messages/AAMkAGI=GRAPH-ID/reply"


def test_send_validation_rejects_extra_fields(app):
    r = app.post(
        "/api/outlook/send",
        json={
            "to": ["doug@gdx.com"], "subject": "hi", "body_html": "<p>hi</p>",
            "secret_admin_flag": True,
        },
    )
    assert r.status_code == 422
