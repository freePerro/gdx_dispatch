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


def _filtered_on(tdb, column_name: str) -> bool:
    """True when some .filter() on the message query mentioned `column_name`.

    Audit round 4: several tests asserted `filter.called`, which is True on
    every request (_load_tech_emails filters the User query on the same shared
    mock) — so they passed with the WHERE clause deleted entirely. Assert the
    predicate's SQL text instead.
    """
    calls = tdb.query.return_value.filter.call_args_list
    return any(c.args and column_name in str(c.args[0]) for c in calls)


def _set_raw_rows(tdb, rows):
    tdb.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = rows


def test_list_messages_returns_visible_only(app):
    client, tdb = app
    msgs = [_msg(linked_customer_id=uuid4()), _msg(linked_customer_id=uuid4())]
    _set_raw_rows(tdb, msgs)
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=msgs):
        r = client.get("/api/outlook/messages")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["subject"] == "Re: estimate"
    assert body["items"][0]["from_address"] == "alice@x.com"


def test_list_messages_full_window_has_more(app):
    """A full raw window that yields visible items → has_more True, next_offset
    advanced by the window size."""
    client, tdb = app
    raw = [_msg() for _ in range(10)]  # full window of 10 (== limit)
    _set_raw_rows(tdb, raw)
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=raw):
        r = client.get("/api/outlook/messages?limit=10&offset=0")
    body = r.json()
    assert len(body["items"]) == 10
    assert body["has_more"] is True
    assert body["next_offset"] == 10


def test_list_messages_skips_fully_hidden_window(app):
    """A window the visibility filter empties is skipped SERVER-SIDE so the
    client never gets a blank 'Load more' page while more rows remain."""
    client, tdb = app
    win1 = [_msg() for _ in range(10)]   # full window, all hidden
    win2 = [_msg(), _msg(), _msg()]      # short window, visible
    tdb.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.side_effect = [win1, win2]
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible",
               side_effect=[[], win2]):
        r = client.get("/api/outlook/messages?limit=10&offset=0")
    body = r.json()
    assert len(body["items"]) == 3       # skipped win1, returned win2's visible
    assert body["has_more"] is False     # win2 was short → end reached
    assert body["next_offset"] == 13     # consumed 10 + 3 raw rows


def test_list_messages_no_more_when_raw_window_short(app):
    client, tdb = app
    raw = [_msg(), _msg()]  # fewer than limit → end reached
    _set_raw_rows(tdb, raw)
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=raw):
        r = client.get("/api/outlook/messages?limit=10&offset=0")
    body = r.json()
    assert body["has_more"] is False
    assert len(body["items"]) == 2


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


# ── P1.1 search ─────────────────────────────────────────────────────────


def test_search_predicate_escapes_like_wildcards():
    """A user searching for "50%" must not match every message. The wildcard
    has to reach SQL escaped, or the search silently returns the whole box."""
    from gdx_dispatch.modules.outlook.views_router import _search_predicate

    sql = str(_search_predicate("50%_off").compile(compile_kwargs={"literal_binds": True}))
    assert "50\\%\\_off" in sql
    assert "lower" in sql.lower() or "ilike" in sql.lower()


def test_list_messages_applies_search_filter(app):
    """?q=… must actually reach SQL as the TERM predicate.

    The first version of this test asserted `tdb.query.return_value.filter.called`
    — which is True on every request, because _load_tech_emails filters the
    User query on the same shared mock. It passed with the search filter
    deleted entirely (audit round 4: the visibility refactor orphaned
    _search_predicate and `?q=` silently returned the whole mailbox). Assert
    the predicate is BUILT from the term and HANDED to the query instead.
    """
    client, tdb = app
    msgs = [_msg()]
    sentinel = object()
    with patch("gdx_dispatch.modules.outlook.views_router._search_predicate",
               return_value=sentinel) as pred, \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=msgs):
        tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = msgs
        r = client.get("/api/outlook/messages?q=furnace")
    assert r.status_code == 200
    pred.assert_called_once_with("furnace")
    # ...and that exact predicate object was passed to .filter()
    assert any(
        call.args and call.args[0] is sentinel
        for call in tdb.query.return_value.filter.call_args_list
    ), "the search predicate never reached the query"


def test_search_without_a_term_does_not_build_a_predicate(app):
    """The unsearched list must not pay for, or accidentally apply, a term
    filter — that path pages over raw rows by design."""
    client, tdb = app
    msgs = [_msg()]
    _set_raw_rows(tdb, msgs)
    with patch("gdx_dispatch.modules.outlook.views_router._search_predicate") as pred, \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=msgs):
        r = client.get("/api/outlook/messages")
    assert r.status_code == 200
    assert not pred.called


def test_list_messages_blank_search_is_not_a_filter(app):
    """q="   " must behave like no search at all — not like a search for
    whitespace (which would match almost nothing)."""
    client, tdb = app
    msgs = [_msg()]
    _set_raw_rows(tdb, msgs)
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=msgs):
        r = client.get("/api/outlook/messages?q=%20%20")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


# ── P2.6 unread count ───────────────────────────────────────────────────


def test_unread_count_counts_visible_only(app):
    """The badge must count what the VIEWER can open, not every unread row —
    otherwise a tech sees a badge for mail that isn't there when they click."""
    client, tdb = app
    rows = [_msg(is_read=False) for _ in range(3)]
    tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows
    with patch("gdx_dispatch.modules.outlook.views_router._unbadged_folder_ids", return_value=[]), \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=rows[:1]):
        r = client.get("/api/outlook/messages/unread-count")
    assert r.status_code == 200
    # No "capped" field: telling a viewer who sees 0 that the scan window was
    # full would disclose the mailbox holds >=500 unread messages.
    assert r.json() == {"count": 1}
    assert _filtered_on(tdb, "is_read")


def test_unread_count_route_not_eaten_by_message_id(app):
    """/messages/unread-count must not be parsed as /messages/{uuid}."""
    client, tdb = app
    tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    with patch("gdx_dispatch.modules.outlook.views_router._unbadged_folder_ids", return_value=[]), \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=[]):
        r = client.get("/api/outlook/messages/unread-count")
    assert r.status_code == 200  # a 422 here means the UUID route swallowed it


# ── 1.3 conversation thread ─────────────────────────────────────────────


def test_thread_returns_siblings_oldest_first(app):
    client, tdb = app
    anchor = _msg(conversation_id="conv-9")
    sibling = _msg(
        conversation_id="conv-9", subject="Re: Re: estimate",
        received_at=datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc),
    )
    tdb.get.return_value = anchor
    tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [anchor, sibling]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible",
               return_value=[anchor, sibling]):
        r = client.get(f"/api/outlook/messages/{anchor.id}/thread")
    assert r.status_code == 200
    # Chronological for a reader, regardless of the SQL window's order.
    assert [m["id"] for m in r.json()] == [str(anchor.id), str(sibling.id)]
    # ...and the query was actually scoped to the conversation, rather than
    # returning whatever the mock had lying around.
    assert _filtered_on(tdb, "conversation_id")


def test_thread_hides_siblings_the_viewer_cannot_see(app):
    """Seeing one message in a thread does NOT grant the rest — a personal
    reply inside a shared thread stays hidden."""
    client, tdb = app
    anchor = _msg(conversation_id="conv-9")
    secret = _msg(conversation_id="conv-9", is_personal=True)
    tdb.get.return_value = anchor
    tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [anchor, secret]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=[anchor]):
        r = client.get(f"/api/outlook/messages/{anchor.id}/thread")
    assert [m["id"] for m in r.json()] == [str(anchor.id)]


def test_thread_without_conversation_id_returns_self(app):
    client, tdb = app
    anchor = _msg(conversation_id=None)
    tdb.get.return_value = anchor
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.get(f"/api/outlook/messages/{anchor.id}/thread")
    assert [m["id"] for m in r.json()] == [str(anchor.id)]


def test_thread_404_when_message_hidden(app):
    client, tdb = app
    anchor = _msg()
    tdb.get.return_value = anchor
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.get(f"/api/outlook/messages/{anchor.id}/thread")
    assert r.status_code == 404


# ── P2.2 email → planner task ───────────────────────────────────────────


def test_create_task_from_message_carries_links(app):
    client, tdb = app
    cust, job = uuid4(), uuid4()
    msg = _msg(subject="Broken spring", linked_customer_id=cust, linked_job_id=job)
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(f"/api/outlook/messages/{msg.id}/create-task", json={})
    assert r.status_code == 201
    assert r.json()["title"] == "Email: Broken spring"
    task = tdb.add.call_args.args[0]
    assert task.customer_id == str(cust)
    assert task.job_id == str(job)
    assert task.source == "email_capture"
    # due_date stamped so the needs-action sort can't bury it (same rule the
    # phone quick-capture path follows).
    assert task.due_date is not None
    assert tdb.commit.called


def test_create_task_404_when_message_hidden(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.post(f"/api/outlook/messages/{msg.id}/create-task", json={})
    assert r.status_code == 404
    assert not tdb.add.called


def test_create_task_rejects_bad_priority(app):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    r = client.post(f"/api/outlook/messages/{msg.id}/create-task", json={"priority": "🔥"})
    assert r.status_code == 422


# ── P2.3 save attachment → job document ─────────────────────────────────


def _job_row(customer_id=None):
    row = MagicMock()
    row.id = uuid4()
    row.customer_id = customer_id
    return row


def test_save_attachment_to_job_creates_document(app, tmp_path):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    job_id = uuid4()
    # 1st .first() → the Job lookup; 2nd → the content-hash dedup lookup (miss).
    tdb.query.return_value.filter.return_value.first.side_effect = [_job_row(), None]
    listing = [{"id": "a1", "name": "po.pdf", "contentType": "application/pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir",
               return_value=tmp_path), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"%PDF"]):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(job_id)},
        )
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "po.pdf"
    assert r.json()["size_bytes"] == 4
    # bytes actually hit disk — a Document row pointing at nothing is worse
    # than no row at all.
    written = list(tmp_path.iterdir())
    assert len(written) == 1 and written[0].read_bytes() == b"%PDF"
    doc = tdb.add.call_args.args[0]
    assert doc.job_id == job_id
    # content_hash is deliberately unset — it is the vendor pipelines'
    # tenant-wide dedup key and would block importing the same PDF as a
    # vendor document forever.
    assert doc.content_hash is None
    # customer_id stays NULL: the customer portal lists documents by
    # customer_id and would publish the filename + email subject.
    assert doc.customer_id is None


def test_save_attachment_to_job_is_idempotent(app, tmp_path):
    """Saving the same attachment twice returns the first Document instead of
    duplicating the bytes on disk."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    existing = MagicMock()
    existing.id = uuid4()
    existing.original_name = "po.pdf"
    existing.file_size = 4
    # 1st .first() → the Job lookup; 2nd → the existing Document.
    tdb.query.return_value.filter.return_value.first.side_effect = [_job_row(), existing]
    listing = [{"id": "a1", "name": "po.pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir",
               return_value=tmp_path), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"%PDF"]):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 201
    assert r.json()["already_saved"] is True
    assert not tdb.add.called
    assert list(tmp_path.iterdir()) == []


def test_save_attachment_422_for_unknown_job(app, tmp_path):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    tdb.query.return_value.filter.return_value.first.return_value = None
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir",
               return_value=tmp_path):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 422


def test_save_attachment_404_when_message_hidden(app, tmp_path):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=False):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 404


# ── audit round 3: privacy regressions the first cut shipped ─────────────


def test_search_cursor_never_counts_hidden_matches(app):
    """THE oracle test. Search pages over VISIBLE rows, so a viewer who can
    see nothing learns nothing — not even how many hidden messages contain
    their guessed word. The old raw cursor returned next_offset = number of
    matching rows INCLUDING hidden ones, which turns ?q= into a content
    oracle over mail the viewer may not open."""
    client, tdb = app
    hidden = [_msg(is_personal=True) for _ in range(7)]
    tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = hidden
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=[]):
        r = client.get("/api/outlook/messages?q=divorce&limit=1&offset=0")
    body = r.json()
    assert body["items"] == []
    assert body["has_more"] is False
    # 0, not 7 — the response must not leak the hidden match count.
    assert body["next_offset"] == 0


def test_search_paginates_over_visible_results(app):
    client, tdb = app
    rows = [_msg() for _ in range(5)]
    tdb.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows
    with patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=rows):
        r = client.get("/api/outlook/messages?q=door&limit=2&offset=0")
    body = r.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_offset"] == 2


def test_create_task_refuses_a_personal_message(app):
    """PlannerTask rows are readable tenant-wide, so copying a message the
    owner marked personal into one would publish its subject to every tech."""
    client, tdb = app
    msg = _msg(is_personal=True, subject="MRI results")
    tdb.get.return_value = msg
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(f"/api/outlook/messages/{msg.id}/create-task", json={})
    assert r.status_code == 409
    assert "personal" in r.text.lower()
    assert not tdb.add.called


def test_create_task_rejects_an_unknown_assignee(app):
    """A task assigned to a non-user is invisible work — it never appears in
    anyone's 'mine' view."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    tdb.query.return_value.filter.return_value.first.return_value = None
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/create-task",
            json={"assigned_to": str(uuid4())},
        )
    assert r.status_code == 422
    assert not tdb.add.called


def test_save_attachment_403_for_a_tech_on_someone_elses_job(app, tmp_path):
    """Seeing an email must not confer the right to write a file onto any job
    in the tenant. Office roles may; a tech only onto their own jobs."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    tdb.query.return_value.filter.return_value.first.return_value = _job_row()
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._user_role", return_value="technician"), \
         patch("gdx_dispatch.core.job_access.job_belongs_to_user", return_value=False), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir", return_value=tmp_path):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 403
    assert list(tmp_path.iterdir()) == []


def test_save_attachment_allows_a_tech_on_their_own_job(app, tmp_path):
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    tdb.query.return_value.filter.return_value.first.side_effect = [_job_row(), None]
    listing = [{"id": "a1", "name": "po.pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._user_role", return_value="technician"), \
         patch("gdx_dispatch.core.job_access.job_belongs_to_user", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir", return_value=tmp_path), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"%PDF"]):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 201


def test_save_attachment_truncates_an_overlong_filename(app, tmp_path):
    """Attachment names come from whoever emailed us. A 300-char name used to
    pass validation, get its bytes written, and THEN blow up on the INSERT
    (String(255)) — leaving an orphan blob that every retry duplicated."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    tdb.query.return_value.filter.return_value.first.side_effect = [_job_row(), None]
    listing = [{"id": "a1", "name": "A" * 300 + ".pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir", return_value=tmp_path), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"%PDF"]):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 201
    doc = tdb.add.call_args.args[0]
    assert len(doc.original_name) <= 255
    assert len(doc.title) <= 255


def test_save_attachment_cleans_up_the_file_when_the_insert_fails(app, tmp_path):
    """No orphan bytes: a failed commit must not leave a file in UPLOAD_DIR
    with no row pointing at it."""
    client, tdb = app
    msg = _msg()
    tdb.get.return_value = msg
    tdb.query.return_value.filter.return_value.first.side_effect = [_job_row(), None]
    tdb.commit.side_effect = RuntimeError("value too long for type character varying(255)")
    listing = [{"id": "a1", "name": "po.pdf", "size": 4}]
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._document_upload_dir", return_value=tmp_path), \
         patch("gdx_dispatch.modules.outlook.views_router._owner_graph",
               side_effect=[listing, b"%PDF"]):
        r = client.post(
            f"/api/outlook/messages/{msg.id}/attachments/a1/save-to-job",
            json={"job_id": str(uuid4())},
        )
    assert r.status_code == 500
    assert list(tmp_path.iterdir()) == []
    assert tdb.rollback.called


def test_unread_count_excludes_junk_and_deleted_folders(app):
    """The badge's click target is the Inbox. Counting Junk there produces a
    badge of 23 that opens onto 4 unread messages — and a "new email" toast
    for every spam that lands."""
    client, tdb = app
    rows = [_msg(is_read=False)]
    chain = tdb.query.return_value.filter.return_value
    chain.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows
    with patch("gdx_dispatch.modules.outlook.views_router._unbadged_folder_ids",
               return_value=["g-junk", "g-trash"]), \
         patch("gdx_dispatch.modules.outlook.views_router.filter_visible", return_value=rows):
        r = client.get("/api/outlook/messages/unread-count")
    assert r.status_code == 200
    assert r.json() == {"count": 1}
    # The exclusion actually reached SQL (a second .filter on the query).
    assert chain.filter.called


def test_detail_exposes_the_mailbox_address_for_reply_all(app):
    """Reply-all must drop the shared inbox's OWN address from Cc, and the
    client can't infer which recipient that is — the server has to say."""
    client, tdb = app
    msg = _msg(to_addresses=["office@gdx.com", "sam@gdx.com"])
    tdb.get.return_value = msg
    account_row = ("Office@GDX.com ",)  # UPN as Graph stores it: mixed case
    tdb.query.return_value.filter.return_value.first.return_value = account_row
    with patch("gdx_dispatch.modules.outlook.views_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.views_router._viewer_owns_mailbox", return_value=True):
        r = client.get(f"/api/outlook/messages/{msg.id}")
    assert r.status_code == 200
    # Normalized for a case-insensitive compare against the recipient list.
    assert r.json()["mailbox_address"] == "office@gdx.com"
