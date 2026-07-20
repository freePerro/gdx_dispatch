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


# ── POST /messages/{id}/personal (owner-only privacy toggle) ────────────


def _wire_msg_and_account(tdb, msg, owner_user_id):
    """tenant_db.get dispatch: OutlookMessage → msg, OutlookAccount → account."""
    account = MagicMock()
    account.user_id = str(owner_user_id)
    def _get(model, pk):
        return msg if model.__name__ == "OutlookMessage" else account
    tdb.get.side_effect = _get
    return account


def test_set_personal_owner_flips_flag_and_persists(app):
    client, tdb = app
    msg = _msg(is_personal=False)
    _wire_msg_and_account(tdb, msg, UID)  # viewer IS the mailbox owner
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(f"/api/outlook/messages/{msg.id}/personal", json={"is_personal": True})
    assert r.status_code == 200
    body = r.json()
    assert body["is_personal"] is True
    assert body["viewer_is_owner"] is True
    assert msg.is_personal is True
    tdb.commit.assert_called()


def test_set_personal_non_owner_403(app):
    client, tdb = app
    msg = _msg(is_personal=False)
    _wire_msg_and_account(tdb, msg, uuid4())  # someone else owns the mailbox
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(f"/api/outlook/messages/{msg.id}/personal", json={"is_personal": True})
    assert r.status_code == 403
    assert msg.is_personal is False
    tdb.commit.assert_not_called()


def test_set_personal_invisible_message_404_not_403(app):
    """Never confirm existence to a viewer the ACL hides the message from."""
    client, tdb = app
    msg = _msg(is_personal=False)
    _wire_msg_and_account(tdb, msg, UID)
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.post(f"/api/outlook/messages/{msg.id}/personal", json={"is_personal": True})
    assert r.status_code == 404


def test_set_personal_unknown_message_404(app):
    client, tdb = app
    tdb.get.side_effect = None
    tdb.get.return_value = None
    r = client.post(f"/api/outlook/messages/{uuid4()}/personal", json={"is_personal": True})
    assert r.status_code == 404


def test_detail_reports_viewer_is_owner(app):
    client, tdb = app
    msg = _msg()
    _wire_msg_and_account(tdb, msg, UID)
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.get(f"/api/outlook/messages/{msg.id}")
    assert r.status_code == 200
    assert r.json()["viewer_is_owner"] is True


def test_detail_viewer_is_owner_false_for_non_owner(app):
    client, tdb = app
    msg = _msg()
    _wire_msg_and_account(tdb, msg, uuid4())
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.get(f"/api/outlook/messages/{msg.id}")
    assert r.status_code == 200
    assert r.json()["viewer_is_owner"] is False


def test_set_personal_hidden_by_real_acl_404(app):
    """No can_view patch — the real can_view PERSONAL branch runs (rules are
    never loaded; is_personal short-circuits first): a personal message owned
    by someone else must 404 (never 403) for a non-owner, pinning the check
    ordering (visibility before ownership) through the genuine chokepoint."""
    client, tdb = app
    msg = _msg(is_personal=True)
    _wire_msg_and_account(tdb, msg, uuid4())  # someone else's mailbox
    r = client.post(f"/api/outlook/messages/{msg.id}/personal", json={"is_personal": False})
    assert r.status_code == 404
    assert msg.is_personal is True  # untouched


# ── GET /messages/{id}/body (D1 live body fetch) ────────────────────────


def _graph_cm(gc):
    """Wrap a mock graph client as a with_outlook_client context manager."""
    cm = MagicMock()
    cm.__enter__.return_value = gc
    cm.__exit__.return_value = False
    return cm


def test_body_fetches_html_for_owner(app):
    client, tdb = app
    msg = _msg()
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    gc = MagicMock()
    gc.get_message.return_value = {"body": {"contentType": "html", "content": "<b>hi</b>"}}
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client", return_value=_graph_cm(gc)):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.status_code == 200
    b = r.json()
    assert b["fetched"] is True
    assert b["content_type"] == "html"
    assert b["body_html"] == "<b>hi</b>"
    gc.get_message.assert_called_once_with("AAMkREMOTE")


def test_body_text_contenttype_preserved(app):
    client, tdb = app
    msg = _msg()
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    gc = MagicMock()
    gc.get_message.return_value = {"body": {"contentType": "text", "content": "plain hi"}}
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client", return_value=_graph_cm(gc)):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.json()["content_type"] == "text"


def test_body_404_when_not_visible(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.status_code == 404


def test_body_404_when_missing(app):
    client, tdb = app
    tdb.get.return_value = None
    r = client.get(f"/api/outlook/messages/{uuid4()}/body")
    assert r.status_code == 404


def test_body_local_draft_no_remote_no_graph_call(app):
    client, tdb = app
    msg = _msg(body_preview="draft text")
    msg.graph_message_id = "local-draft-abc"
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client") as woc:
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.status_code == 200
    b = r.json()
    assert b["fetched"] is False
    assert b["reason"] == "no_remote_copy"
    assert b["body_preview"] == "draft text"
    woc.assert_not_called()  # never touch Graph for a local draft


def test_body_no_owner_falls_back(app):
    client, tdb = app
    msg = _msg()
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=None):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.json()["reason"] == "no_account_owner"


def test_body_reconnect_required_falls_back_to_preview(app):
    from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired
    client, tdb = app
    msg = _msg(body_preview="the preview")
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client",
               side_effect=OutlookReconnectRequired("reconnect")):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.status_code == 200
    b = r.json()
    assert b["fetched"] is False
    assert b["reason"] == "reconnect_required"
    assert b["body_preview"] == "the preview"


def test_body_graph_404_reports_message_gone(app):
    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
    client, tdb = app
    msg = _msg()
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    gc = MagicMock()
    gc.get_message.side_effect = OutlookGraphAPIError(404, "not found")
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client", return_value=_graph_cm(gc)):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.json()["reason"] == "message_gone"


def test_body_empty_content_reports_empty(app):
    client, tdb = app
    msg = _msg()
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    gc = MagicMock()
    gc.get_message.return_value = {"body": {"contentType": "html", "content": ""}}
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client", return_value=_graph_cm(gc)):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.json()["reason"] == "empty_body"


def test_body_transient_retry_reissues_once(app):
    from gdx_dispatch.modules.outlook.token_refresh import OutlookTransientRetry
    client, tdb = app
    msg = _msg()
    msg.graph_message_id = "AAMkREMOTE"
    tdb.get.return_value = msg
    gc = MagicMock()
    gc.get_message.return_value = {"body": {"contentType": "html", "content": "<p>ok</p>"}}
    calls = {"n": 0}

    def _woc(*a, **k):
        # First open raises the transient-retry contract, second succeeds.
        calls["n"] += 1
        if calls["n"] == 1:
            raise OutlookTransientRetry("401 mid-call")
        return _graph_cm(gc)

    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.with_outlook_client", side_effect=_woc):
        r = client.get(f"/api/outlook/messages/{msg.id}/body")
    assert r.status_code == 200
    assert r.json()["fetched"] is True
    assert calls["n"] == 2  # retried exactly once


# ── POST/DELETE /messages/{id}/link (D3 manual tag) ─────────────────────


def _as_role(client, role):
    client.app.dependency_overrides[get_user_for_views] = lambda: {
        "user_id": str(UID), "tenant_id": str(TID), "role": role,
    }


def test_link_sets_manual_tag(app):
    client, tdb = app
    msg = _msg()
    _wire_msg_and_account(tdb, msg, UID)
    cid = uuid4()
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(f"/api/outlook/messages/{msg.id}/link", json={"customer_id": str(cid)})
    assert r.status_code == 200
    assert str(msg.linked_customer_id) == str(cid)
    assert msg.tag_strategy == "manual"


def test_link_requires_customer_or_job(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    r = client.post(f"/api/outlook/messages/{msg.id}/link", json={})
    assert r.status_code == 422


def test_link_forbidden_for_tech(app):
    client, tdb = app
    _as_role(client, "technician")
    msg = _msg()
    tdb.get.return_value = msg
    r = client.post(f"/api/outlook/messages/{msg.id}/link", json={"customer_id": str(uuid4())})
    assert r.status_code == 403


def test_link_404_when_not_visible(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.post(f"/api/outlook/messages/{msg.id}/link", json={"job_id": str(uuid4())})
    assert r.status_code == 404


def test_link_422_for_unknown_customer(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    # Customer lookup returns None → 422 (not a 500 on insert).
    tdb.query.return_value.filter.return_value.first.return_value = None
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(f"/api/outlook/messages/{msg.id}/link", json={"customer_id": str(uuid4())})
    assert r.status_code == 422


def test_unlink_pins_as_manual_no_link(app):
    client, tdb = app
    msg = _msg(linked_customer_id=uuid4())
    msg.tag_strategy = "auto_match"
    _wire_msg_and_account(tdb, msg, UID)
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.delete(f"/api/outlook/messages/{msg.id}/link")
    assert r.status_code == 200
    assert msg.linked_customer_id is None
    assert msg.linked_job_id is None
    # Pinned 'manual' (not NULL) so the hourly retag can't re-apply the auto-tag.
    assert msg.tag_strategy == "manual"


def test_unlink_forbidden_for_viewer(app):
    client, tdb = app
    _as_role(client, "viewer")
    msg = _msg()
    tdb.get.return_value = msg
    r = client.delete(f"/api/outlook/messages/{msg.id}/link")
    assert r.status_code == 403


# ── attachments list + download (D4) ────────────────────────────────────


def _patch_owner_graph(return_value=None, side_effect=None):
    return patch(
        "gdx_dispatch.modules.outlook.views_router._owner_graph",
        return_value=return_value, side_effect=side_effect,
    )


def test_attachments_list_ok(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    graph_atts = [
        {"id": "a1", "name": "quote.pdf", "contentType": "application/pdf", "size": 1024, "isInline": False},
        {"id": "a2", "name": None, "contentType": "image/png", "size": 50, "isInline": True},
        {"name": "noid.txt"},  # dropped — no id
    ]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         _patch_owner_graph(return_value=graph_atts):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments")
    assert r.status_code == 200
    body = r.json()
    assert body["fetched"] is True
    assert [a["id"] for a in body["attachments"]] == ["a1", "a2"]
    assert body["attachments"][0]["content_type"] == "application/pdf"
    assert body["attachments"][1]["is_inline"] is True


def test_attachments_list_404_when_not_visible(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments")
    assert r.status_code == 404


def test_attachments_list_reconnect_falls_back(app):
    from gdx_dispatch.modules.outlook.views_router import _OwnerFetchError
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         _patch_owner_graph(side_effect=_OwnerFetchError("reconnect_required")):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments")
    assert r.status_code == 200
    assert r.json() == {"fetched": False, "attachments": [], "reason": "reconnect_required"}


def test_attachment_download_streams_bytes(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    listing = [{"id": "a1", "name": "quote.pdf", "contentType": "application/pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"%PDF"]):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments/a1")
    assert r.status_code == 200
    assert r.content == b"%PDF"
    assert r.headers["content-type"].startswith("application/pdf")
    assert 'filename="quote.pdf"' in r.headers["content-disposition"]


def test_attachment_download_404_for_unknown_id(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    listing = [{"id": "a1", "name": "quote.pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         _patch_owner_graph(return_value=listing):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments/NOPE")
    assert r.status_code == 404


def test_attachment_download_413_when_too_large(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    listing = [{"id": "a1", "name": "big.zip", "size": 999 * 1024 * 1024}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         _patch_owner_graph(return_value=listing):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments/a1")
    assert r.status_code == 413


def test_attachment_download_sanitizes_filename_header(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    listing = [{"id": "a1", "name": 'ev"il\r\nX-Injected: 1.pdf', "size": 3}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"pdf"]):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments/a1")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert "injected" not in {k.lower() for k in r.headers}  # not a real header


def test_attachment_download_nonascii_filename_no_500(app):
    """A CJK/accented filename must not 500 on latin-1 header encoding —
    RFC 5987 filename* carries UTF-8, filename= is an ASCII fallback."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    listing = [{"id": "a1", "name": "契約書.pdf", "size": 3}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"pdf"]):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments/a1")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd
    assert "%E5%A5%91" in cd or "%" in cd  # percent-encoded utf-8


def test_attachments_list_excludes_item_and_reference(app):
    """Only fileAttachments have downloadable bytes; item/reference are hidden."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    graph_atts = [
        {"id": "a1", "name": "real.pdf", "size": 10, "@odata.type": "#microsoft.graph.fileAttachment"},
        {"id": "a2", "name": "fwd.eml", "@odata.type": "#microsoft.graph.itemAttachment"},
        {"id": "a3", "name": "onedrive", "@odata.type": "#microsoft.graph.referenceAttachment"},
    ]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         _patch_owner_graph(return_value=graph_atts):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments")
    assert [a["id"] for a in r.json()["attachments"]] == ["a1"]


def test_attachment_download_502_on_graph_error(app):
    from gdx_dispatch.modules.outlook.views_router import _OwnerFetchError
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         _patch_owner_graph(side_effect=_OwnerFetchError("graph_error")):
        r = client.get(f"/api/outlook/messages/{msg.id}/attachments/a1")
    assert r.status_code == 502
