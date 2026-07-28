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
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.mailbox_owner_id", return_value=str(UID)), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True):
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


def test_send_reply_hidden_parent_falls_back_to_plain_send(app):
    """ACL-hidden reply parent behaves exactly like a MISSING one: plain
    /me/sendMail, no threading — so reply-vs-sendMail can't be used as an
    existence oracle for personal/owner_only mail (audit round 2)."""
    parent_uuid = uuid4()
    fake_parent = MagicMock()
    fake_parent.graph_message_id = "AAMkAGI=GRAPH-ID"
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=False):
        ctx.return_value.__enter__.return_value = fake_gc
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
    method, path = fake_gc._request.call_args.args[0:2]
    assert path == "/me/sendMail"  # identical to nonexistent-parent behavior


def test_send_validation_rejects_extra_fields(app):
    r = app.post(
        "/api/outlook/send",
        json={
            "to": ["doug@gdx.com"], "subject": "hi", "body_html": "<p>hi</p>",
            "secret_admin_flag": True,
        },
    )
    assert r.status_code == 422


# ── P2.5 job thread marker ──────────────────────────────────────────────


def test_job_marked_subject_appends_marker():
    """The marker is what makes the customer's REPLY auto-link back to the
    job — tagger._JOB_PATTERNS reads exactly this shape off the subject."""
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    job = uuid4()
    out = job_marked_subject("Your door quote", job)
    assert out == f"Your door quote [Job #{job}]"


def test_job_marked_subject_is_idempotent():
    """A long thread must not accumulate one marker per round-trip."""
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    job = uuid4()
    once = job_marked_subject("Quote", job)
    assert job_marked_subject(once, job) == once


def test_job_marked_subject_noop_without_job():
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    assert job_marked_subject("Plain mail", None) == "Plain mail"


def test_job_marked_subject_stays_within_rfc_limit():
    """Trim the human part, never the marker — an over-length subject is a
    Graph 400 that would fail the whole send."""
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    job = uuid4()
    out = job_marked_subject("x" * 1200, job)
    assert len(out) <= 998
    assert out.endswith(f"[Job #{job}]")


def test_send_with_job_id_stamps_subject(app):
    """No job_number resolvable → the marker falls back to the uuid form,
    which the tagger also matches."""
    job = uuid4()
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router._job_number", return_value=None):
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post(
            "/api/outlook/send",
            json={
                "to": ["cust@x.com"], "subject": "Your quote",
                "body_html": "<p>hi</p>", "job_id": str(job),
            },
        )
    assert r.status_code == 200
    sent = fake_gc._request.call_args.kwargs["json"]
    assert sent["message"]["subject"] == f"Your quote [Job #{job}]"


def test_send_reply_non_owner_falls_back_to_plain_send(app):
    """A shared-mailbox viewer who is NOT the owner cannot reply through
    /me/messages/{owner's id}/reply — that id doesn't exist in their mailbox
    and Graph 404s, 502ing the whole send. Degrade to sendMail: the reply
    still goes out, it just loses RFC threading."""
    parent_uuid = uuid4()
    fake_parent = MagicMock()
    fake_parent.graph_message_id = "AAMkAGI=GRAPH-ID"
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.mailbox_owner_id",
               return_value=str(uuid4())), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True):
        ctx.return_value.__enter__.return_value = fake_gc
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
    assert fake_gc._request.call_args.args[1] == "/me/sendMail"


# ── 1.4 forward ─────────────────────────────────────────────────────────


def _fake_msg(graph_id="AAMkAGI=GRAPH-ID", is_personal=False):
    m = MagicMock()
    m.graph_message_id = graph_id
    # Explicit: a bare MagicMock attribute is TRUTHY, so leaving is_personal
    # unset would make every message look "personal" to the new guards.
    m.is_personal = is_personal
    m.subject = "Broken spring"
    m.from_address = "alice@example.com"
    m.body_preview = "door wont open"
    return m


def _graph_response(payload):
    """graph_client._request returns an httpx.Response, not parsed JSON —
    mock that shape or a test passes against an API contract that doesn't
    exist (this is exactly how the null-draft-id bug hid)."""
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def test_forward_calls_graph_forward_action(app):
    """Native /forward, not a re-compose — Graph carries the original
    attachments so we never round-trip the bytes."""
    mid = uuid4()
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.send_router.mailbox_owner_id", return_value=str(UID)):
        ctx.return_value.__enter__.return_value = fake_gc
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(
            f"/api/outlook/messages/{mid}/forward",
            json={"to": ["sub@x.com"], "comment": "FYI"},
        )
    assert r.status_code == 200
    method, path = fake_gc._request.call_args.args[0:2]
    assert (method, path) == ("POST", "/me/messages/AAMkAGI=GRAPH-ID/forward")
    body = fake_gc._request.call_args.kwargs["json"]
    assert body["comment"] == "FYI"
    assert body["toRecipients"] == [{"emailAddress": {"address": "sub@x.com"}}]


def test_forward_is_owner_only(app):
    """/me/messages/{id}/forward resolves against the CALLER's mailbox and
    would send under the owner's name — 403 a non-owner rather than let them
    mail from someone else's account."""
    mid = uuid4()
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True), \
         patch("gdx_dispatch.modules.outlook.send_router.mailbox_owner_id",
               return_value=str(uuid4())):
        ctx.return_value.__enter__.return_value = fake_gc
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(f"/api/outlook/messages/{mid}/forward", json={"to": ["sub@x.com"]})
    assert r.status_code == 403
    assert not fake_gc._request.called


def test_forward_404_when_message_hidden(app):
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=False):
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(f"/api/outlook/messages/{mid}/forward", json={"to": ["sub@x.com"]})
    assert r.status_code == 404


def test_forward_409_without_server_copy(app):
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True):
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg(
            graph_id="local-draft-123"
        )
        r = app.post(f"/api/outlook/messages/{mid}/forward", json={"to": ["sub@x.com"]})
    assert r.status_code == 409


# ── 1.5 drafts ──────────────────────────────────────────────────────────


def test_create_draft_posts_to_me_messages(app):
    """A REAL Graph draft — so it round-trips into the Drafts folder the
    rail already renders."""
    fake_gc = MagicMock()
    fake_gc._request.return_value = _graph_response({"id": "DRAFT-1", "webLink": "https://outlook/x"})
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post(
            "/api/outlook/drafts",
            json={"to": ["a@b.com"], "subject": "Draft me", "body_html": "<p>wip</p>"},
        )
    assert r.status_code == 201
    method, path = fake_gc._request.call_args.args[0:2]
    assert (method, path) == ("POST", "/me/messages")
    assert r.json()["graph_message_id"] == "DRAFT-1"


def test_create_draft_allows_empty_fields(app):
    """Saving a half-written message is the entire point of a draft."""
    fake_gc = MagicMock()
    fake_gc._request.return_value = _graph_response({"id": "DRAFT-2"})
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post("/api/outlook/drafts", json={})
    assert r.status_code == 201


def test_create_draft_409_on_reconnect(app):
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.side_effect = OutlookReconnectRequired("no token")
        r = app.post("/api/outlook/drafts", json={"subject": "x"})
    assert r.status_code == 409


def test_create_draft_stamps_job_marker(app):
    job = uuid4()
    fake_gc = MagicMock()
    fake_gc._request.return_value = _graph_response({"id": "DRAFT-3"})
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router._job_number", return_value="JOB-2026-014"):
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post("/api/outlook/drafts", json={"subject": "Quote", "job_id": str(job)})
    assert r.status_code == 201
    # Human-readable number, NOT a raw uuid — this subject reaches the customer.
    subject = fake_gc._request.call_args.kwargs["json"]["subject"]
    assert subject == "Quote [Job #JOB-2026-014]"
    assert str(job) not in subject


# ── P2.4 AI draft ───────────────────────────────────────────────────────


def test_ai_draft_returns_provider_text(app):
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True), \
         patch("gdx_dispatch.core.ai_provider.generate_sync", return_value="  Sure thing.  "):
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(f"/api/outlook/messages/{mid}/ai-draft", json={})
    assert r.status_code == 200
    assert r.json() == {"draft_text": "Sure thing.", "source": "ai"}


def test_ai_draft_falls_back_when_provider_unavailable(app):
    """No AI configured must not dead-end the button."""
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True), \
         patch("gdx_dispatch.core.ai_provider.generate_sync", return_value=None):
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(f"/api/outlook/messages/{mid}/ai-draft", json={})
    assert r.status_code == 200
    assert r.json()["source"] == "fallback"
    assert r.json()["draft_text"]


def test_ai_draft_survives_provider_exception(app):
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True), \
         patch("gdx_dispatch.core.ai_provider.generate_sync", side_effect=RuntimeError("boom")):
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(f"/api/outlook/messages/{mid}/ai-draft", json={})
    assert r.status_code == 200
    assert r.json()["source"] == "fallback"


def test_ai_draft_404_when_message_hidden(app):
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=False):
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(f"/api/outlook/messages/{mid}/ai-draft", json={})
    assert r.status_code == 404


# ── audit round 3 ────────────────────────────────────────────────────────


def test_create_draft_parses_the_graph_response(app):
    """_request returns an httpx.Response, not JSON. Without .json() the draft
    is created but DraftOut comes back all-null — and a MagicMock'd _request
    hides it, because a mock is not a dict either."""
    fake_gc = MagicMock()
    fake_gc._request.return_value = _graph_response({"id": "REAL-ID", "webLink": "https://outlook/x"})
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post("/api/outlook/drafts", json={"subject": "x"})
    assert r.json()["graph_message_id"] == "REAL-ID"
    assert r.json()["web_link"] == "https://outlook/x"


def test_create_draft_survives_an_unparseable_response(app):
    """The draft exists in Graph either way — don't turn a parse problem into
    a failed save the user will retry."""
    fake_gc = MagicMock()
    resp = MagicMock()
    resp.json.side_effect = ValueError("not json")
    fake_gc._request.return_value = resp
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post("/api/outlook/drafts", json={"subject": "x"})
    assert r.status_code == 201
    assert r.json()["ok"] is True


def test_reply_never_carries_the_job_marker(app):
    """Deterministic marker: NEW mail is stamped, replies never are. Otherwise
    the same reply gets a different customer-visible subject depending on
    whether the sender happens to own the mailbox (owner → Graph /reply,
    non-owner → sendMail)."""
    job = uuid4()
    parent_uuid = uuid4()
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.mailbox_owner_id", return_value=str(uuid4())), \
         patch("gdx_dispatch.modules.outlook.send_router._job_number", return_value="JOB-2026-014"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True):
        ctx.return_value.__enter__.return_value = fake_gc
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg()
        r = app.post(
            "/api/outlook/send",
            json={
                "to": ["a@b.com"], "subject": "Re: your quote",
                "body_html": "<p>x</p>", "in_reply_to": str(parent_uuid),
                "job_id": str(job),
            },
        )
    assert r.status_code == 200
    sent = fake_gc._request.call_args.kwargs["json"]
    assert sent["message"]["subject"] == "Re: your quote"


def test_send_marker_prefers_the_human_readable_job_number(app):
    job = uuid4()
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.send_router._job_number", return_value="JOB-2026-014"):
        ctx.return_value.__enter__.return_value = fake_gc
        r = app.post(
            "/api/outlook/send",
            json={"to": ["c@x.com"], "subject": "Your quote",
                  "body_html": "<p>hi</p>", "job_id": str(job)},
        )
    assert r.status_code == 200
    subject = fake_gc._request.call_args.kwargs["json"]["message"]["subject"]
    assert subject == "Your quote [Job #JOB-2026-014]"
    assert str(job) not in subject  # no raw uuid in customer-facing mail


def test_job_marked_subject_does_not_double_mark_a_uuid_thread():
    """A thread already carrying the older uuid form must not pick up a second
    marker when the number form is available."""
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    job = uuid4()
    already = f"Quote [Job #{job}]"
    assert job_marked_subject(already, job, "JOB-2026-014") == already


def test_ai_draft_refuses_a_personal_message(app):
    """A message the owner marked personal is not shipped to an external
    model, even by the owner's own click."""
    mid = uuid4()
    with patch("gdx_dispatch.modules.outlook.send_router.OutlookMessage"), \
         patch("gdx_dispatch.modules.outlook.send_router.can_view", return_value=True), \
         patch("gdx_dispatch.core.ai_provider.generate_sync") as gen:
        tdb_mock = app.app.dependency_overrides[get_db_for_send]()
        tdb_mock.query.return_value.filter.return_value.one_or_none.return_value = _fake_msg(is_personal=True)
        r = app.post(f"/api/outlook/messages/{mid}/ai-draft", json={})
    assert r.status_code == 409
    assert not gen.called  # nothing left the building


# ── audit round 4 ────────────────────────────────────────────────────────


def test_create_draft_does_not_report_success_on_a_transport_failure(app):
    """A blanket except around the SEND reported ok=True for a connection
    error too — telling the user their draft was safe in Outlook when nothing
    was ever sent. Only the response PARSE is allowed to degrade."""
    with patch("gdx_dispatch.modules.outlook.send_router.with_outlook_client") as ctx:
        ctx.return_value.__enter__.side_effect = ConnectionError("network down")
        with pytest.raises(ConnectionError):
            app.post("/api/outlook/drafts", json={"subject": "x"})


def test_job_marker_is_not_suppressed_by_a_coincidental_substring():
    """A job numbered "14" must still get a marker on "Quote for 14 doors".
    The idempotency check looks for the BRACKETED marker, not a bare token
    scan (which silently disabled the marker for short job numbers)."""
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    job = uuid4()
    out = job_marked_subject("Quote for 14 doors", job, "14")
    assert out == "Quote for 14 doors [Job #14]"


def test_job_marker_still_idempotent_on_the_bracketed_form():
    from gdx_dispatch.modules.outlook.send_router import job_marked_subject

    job = uuid4()
    once = job_marked_subject("Quote", job, "JOB-2026-014")
    assert job_marked_subject(once, job, "JOB-2026-014") == once
