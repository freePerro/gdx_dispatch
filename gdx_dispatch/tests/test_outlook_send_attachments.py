"""Inbox send / forward with picked attachments — through the real routes,
on a real SQLite tenant DB, so the audit row is a row and not a mock call.

Graph itself is a MagicMock here (test_outlook_graph_send.py drives the real
client over a mock transport); what is pinned is the route's contract:
attachments reach Graph, an unreachable pick sends nothing, and every send
leaves an audit row naming who sent which files.
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models import tenant_models  # noqa: F401
from gdx_dispatch.modules.outlook import send_router as sr
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.proposals import models as proposal_models  # noqa: F401

UID, TID = uuid4(), uuid4()


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "technician"}


@pytest.fixture()
def tdb(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.sqlite3'}", connect_args={"check_same_thread": False})
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(tdb, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": str(TID)}
        return await call_next(request)

    app.include_router(sr.router)
    app.dependency_overrides[sr.get_user_for_send] = _user
    app.dependency_overrides[sr.get_db_for_send] = lambda: tdb
    app.dependency_overrides[require_module("email")] = lambda: None
    return TestClient(app)


def _upload(name="door.jpg", data=b"JPEGDATA"):
    return {"kind": "upload", "name": name, "content_type": "image/jpeg",
            "content_base64": base64.b64encode(data).decode()}


def _audits(tdb, action):
    return tdb.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def test_send_with_a_device_file_reaches_graph_and_is_audited(client, tdb):
    gc = MagicMock()
    with patch.object(sr, "with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = gc
        r = client.post("/api/outlook/send", json={
            "to": ["c@example.com"], "subject": "Photos", "body_html": "<p>x</p>",
            "attachment_refs": [_upload()],
        })
    assert r.status_code == 200, r.text
    path = gc._request.call_args.args[1]
    sent = gc._request.call_args.kwargs["json"]["message"]
    assert path == "/me/sendMail"
    assert sent["attachments"][0]["name"] == "door.jpg"
    assert base64.b64decode(sent["attachments"][0]["contentBytes"]) == b"JPEGDATA"
    (row,) = _audits(tdb, "outlook_mail_sent")
    assert row.user_id == str(UID)
    assert row.details["attachments"] == [
        {"name": "door.jpg", "size_bytes": 8, "kind": "upload", "ref_id": None}
    ]
    assert row.details["recipient_count"] == 1


def test_plain_send_is_audited_too(client, tdb):
    with patch.object(sr, "with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = MagicMock()
        r = client.post("/api/outlook/send", json={
            "to": ["c@example.com"], "subject": "hi", "body_html": "<p>x</p>",
        })
    assert r.status_code == 200
    (row,) = _audits(tdb, "outlook_mail_sent")
    assert row.details["attachments"] == []


def test_unreachable_pick_sends_nothing(client, tdb):
    with patch.object(sr, "with_outlook_client") as ctx:
        r = client.post("/api/outlook/send", json={
            "to": ["c@example.com"], "subject": "hi", "body_html": "<p>x</p>",
            "attachment_refs": [{"kind": "estimate_pdf", "id": str(uuid4())}],
        })
    assert r.status_code == 404
    assert "Attachment not found" in r.json()["detail"]
    ctx.assert_not_called()
    assert _audits(tdb, "outlook_mail_sent") == []


def test_forward_with_an_added_file_goes_through_create_forward(client, tdb):
    mid = uuid4()
    gc = MagicMock()
    draft = MagicMock()
    draft.json.return_value = {"id": "D1"}
    gc._request.return_value = draft
    msg = MagicMock(graph_message_id="GRAPH-ID")
    with patch.object(sr, "with_outlook_client") as ctx, \
         patch.object(sr, "can_view", return_value=True), \
         patch.object(sr, "mailbox_owner_id", return_value=str(UID)), \
         patch.object(sr, "OutlookMessage"), \
         patch.object(tdb, "query") as q:
        q.return_value.filter.return_value.one_or_none.return_value = msg
        q.return_value.filter.return_value.all.return_value = []
        ctx.return_value.__enter__.return_value = gc
        r = client.post(f"/api/outlook/messages/{mid}/forward", json={
            "to": ["x@example.com"], "comment": "see photo", "attachment_refs": [_upload()],
        })
    assert r.status_code == 200, r.text
    paths = [c.args[1] for c in gc._request.call_args_list]
    assert paths == [
        "/me/messages/GRAPH-ID/createForward",
        "/me/messages/D1/attachments",
        "/me/messages/D1/send",
    ]
    (row,) = _audits(tdb, "outlook_mail_forwarded")
    assert row.entity_id == str(mid)
    assert row.details["route"] == "draft"
    assert row.details["attachments"][0]["name"] == "door.jpg"


def test_the_legacy_attachments_list_answers_to_the_same_total_cap(client, tdb, monkeypatch):
    # Over Graph's 4MB request this list goes through a draft now, so nothing
    # but the shared cap stops it growing.
    monkeypatch.setattr(sr, "MAX_EMAIL_ATTACHMENTS_BYTES", 10)
    with patch.object(sr, "with_outlook_client") as ctx:
        r = client.post("/api/outlook/send", json={
            "to": ["c@example.com"], "subject": "hi", "body_html": "<p>x</p>",
            "attachments": [{"name": "a.txt", "content_type": "text/plain",
                             "content_base64": base64.b64encode(b"x" * 11).decode()}],
        })
    assert r.status_code == 413
    assert "Remove a file" in r.json()["detail"]
    ctx.assert_not_called()
    assert _audits(tdb, "outlook_mail_sent") == []


def _send_failing_with(client, exc):
    gc = MagicMock()
    gc._request.side_effect = exc
    with patch.object(sr, "with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = gc
        return client.post("/api/outlook/send", json={
            "to": ["c@example.com"], "subject": "hi", "body_html": "<p>x</p>",
        })


@pytest.mark.parametrize("exc", [
    OutlookGraphAPIError(504, "gateway timeout", endpoint="/me/sendMail"),
    OutlookGraphAPIError(503, "unavailable", endpoint="/me/sendMail"),
    httpx.ReadTimeout("timed out"),
])
def test_an_unconfirmed_send_says_check_sent_items_not_rejected(client, tdb, exc):
    # The sendMail call itself failed without an answer: Graph may have sent
    # it, and "rejected" or "try again" would invite a duplicate send.
    r = _send_failing_with(client, exc)
    assert r.status_code == 502
    assert "check Sent Items" in r.json()["detail"]
    assert "rejected" not in r.json()["detail"]


def test_a_refused_send_still_says_rejected(client, tdb):
    r = _send_failing_with(client, OutlookGraphAPIError(400, "bad", endpoint="/me/sendMail"))
    assert r.status_code == 502
    assert r.json()["detail"] == "Microsoft Graph rejected send: 400"


def _big_send(client):
    return client.post("/api/outlook/send", json={
        "to": ["c@example.com"], "subject": "hi", "body_html": "<p>x</p>",
        "attachment_refs": [_upload("big.pdf", b"x" * 2_600_000)],
    })


def test_a_draft_that_fails_before_sending_says_nothing_was_sent(client, tdb):
    # Over the inline budget: draft route. A 503 on the draft create is before
    # anything could go out — don't send anyone hunting Sent Items.
    gc = MagicMock()
    gc._request.side_effect = OutlookGraphAPIError(503, "unavailable", endpoint="/me/messages")
    with patch.object(sr, "with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = gc
        r = _big_send(client)
    assert r.status_code == 502
    assert "nothing was sent" in r.json()["detail"]
    assert "Sent Items" not in r.json()["detail"]
    assert [c.args[1] for c in gc._request.call_args_list] == ["/me/messages"]


def test_a_sign_in_timeout_says_nothing_was_sent(client, tdb):
    with patch.object(sr, "with_outlook_client") as ctx:
        ctx.return_value.__enter__.side_effect = httpx.ConnectTimeout("login timed out")
        r = _big_send(client)
    assert r.status_code == 504
    assert "nothing was sent" in r.json()["detail"]


def test_a_token_refreshed_mid_send_is_answered_not_a_bare_500(client, tdb):
    with patch.object(sr, "with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = MagicMock()
        ctx.return_value.__exit__.side_effect = sr.OutlookTransientRetry("refreshed")
        r = client.post("/api/outlook/send", json={
            "to": ["c@example.com"], "subject": "hi", "body_html": "<p>x</p>",
        })
    assert r.status_code == 503
    assert "did not go out" in r.json()["detail"]
    assert _audits(tdb, "outlook_mail_sent") == []
