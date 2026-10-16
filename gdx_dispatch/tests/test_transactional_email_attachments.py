"""Attachment plumbing for transactional email (2026-07-20).

Why this exists: the invoice /send endpoint (Billing bulk-send, mobile
re-send) and estimate /send emailed an HTML body whose copy referenced an
"attached" PDF — but no path in the chain could carry an attachment, so a
real customer received an invoice email with no invoice PDF. These tests
pin the new attachments contract end to end:

  1. email_sender.send_email builds multipart/mixed MIME with the PDF part
     (and stays multipart/alternative when there are no attachments).
  2. send_transactional_email threads attachments into the SMTP fallback.
  3. _try_outlook_graph ships them in Graph fileAttachment wire shape —
     the same shape modules/outlook/send_router uses.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager

import gdx_dispatch.core.email_sender as email_sender
import gdx_dispatch.core.transactional_email as tx

PDF_BYTES = b"%PDF-1.4 fake-invoice-bytes"
PDF_ATT = {
    "name": "invoice-INV-1.pdf",
    "content_type": "application/pdf",
    "content_base64": base64.b64encode(PDF_BYTES).decode("ascii"),
}

SMTP_CONFIG = {
    "provider": "smtp",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "username": "mailer",
    "password_enc": base64.b64encode(b"hunter2").decode("ascii"),
    "from_email": "billing@example.com",
    "from_name": "GDX Billing",
}


class _FakeSMTP:
    sent_messages: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent_messages.append(msg)


def _patch_smtp(monkeypatch):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender, "get_email_config", lambda db, tid: dict(SMTP_CONFIG))
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)


def test_send_email_attaches_pdf_as_mixed_mime(monkeypatch):
    _patch_smtp(monkeypatch)

    ok = email_sender.send_email(
        db=None, tenant_id="t1", to_email="cust@example.com",
        subject="Invoice", html_body="<p>hi</p>", to_name="Cust",
        attachments=[PDF_ATT],
    )

    assert ok is True
    (msg,) = _FakeSMTP.sent_messages
    assert msg.get_content_type() == "multipart/mixed"
    parts = msg.get_payload()
    # The body travels as a text+html alternative pair inside the mixed
    # envelope (plain-text part added with the shared email shell).
    assert parts[0].get_content_type() == "multipart/alternative"
    alt_parts = parts[0].get_payload()
    assert [p.get_content_type() for p in alt_parts] == ["text/plain", "text/html"]
    pdf_part = parts[1]
    assert pdf_part.get_content_type() == "application/pdf"
    assert pdf_part.get_filename() == "invoice-INV-1.pdf"
    assert pdf_part.get_payload(decode=True) == PDF_BYTES


def test_send_email_without_attachments_stays_alternative(monkeypatch):
    _patch_smtp(monkeypatch)

    ok = email_sender.send_email(
        db=None, tenant_id="t1", to_email="cust@example.com",
        subject="Invoice", html_body="<p>hi</p>",
    )

    assert ok is True
    (msg,) = _FakeSMTP.sent_messages
    assert msg.get_content_type() == "multipart/alternative"


def test_transactional_email_threads_attachments_to_smtp(monkeypatch):
    captured = {}

    def fake_send_email(db, tenant_id, to_email, subject, html_body, to_name="", attachments=None):
        captured["attachments"] = attachments
        return True

    monkeypatch.setattr(email_sender, "get_email_config", lambda db, tid: dict(SMTP_CONFIG))
    monkeypatch.setattr(email_sender, "send_email", fake_send_email)
    # No user/tenant UUIDs → the Outlook branch is skipped entirely, so this
    # exercises the SMTP fallback path in isolation.
    sent, provider, reason = tx.send_transactional_email(
        tenant_db=None, tenant_id="not-a-uuid", user_id=None,
        to_email="cust@example.com", to_name="Cust",
        subject="Invoice", html_body="<p>hi</p>",
        attachments=[PDF_ATT],
    )

    assert (sent, provider, reason) == (True, "smtp", None)
    assert captured["attachments"] == [PDF_ATT]


def test_outlook_graph_body_carries_file_attachments(monkeypatch):
    from uuid import uuid4

    captured = {}

    class _FakeGraphClient:
        def _request(self, method, path, json=None, retry_on=None, **_kw):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = json
            captured["retry_on"] = retry_on

    @contextmanager
    def fake_with_outlook_client(control_db, tenant_db, user_id, tenant_id):
        yield _FakeGraphClient()

    import gdx_dispatch.modules.outlook.token_refresh as token_refresh
    monkeypatch.setattr(token_refresh, "with_outlook_client", fake_with_outlook_client)

    @contextmanager
    def fake_session_local():
        yield None

    import gdx_dispatch.core.database as core_db
    monkeypatch.setattr(core_db, "SessionLocal", fake_session_local)

    sent, reason = tx._try_outlook_graph(
        tenant_db=None, tenant_id=uuid4(), user_id=uuid4(),
        to_email="cust@example.com", subject="Invoice",
        html_body="<p>hi</p>", attachments=[PDF_ATT],
    )

    assert (sent, reason) == (True, None)
    # The call that delivers is not retried past a 429 (GDXA-1318): a 504
    # may follow a send Graph already did.
    from gdx_dispatch.modules.outlook.graph_client import RETRY_THROTTLED_ONLY
    assert captured["path"] == "/me/sendMail"
    assert captured["retry_on"] == RETRY_THROTTLED_ONLY
    atts = captured["body"]["message"]["attachments"]
    assert atts == [{
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "invoice-INV-1.pdf",
        "contentType": "application/pdf",
        "contentBytes": PDF_ATT["content_base64"],
    }]


def test_outlook_graph_body_omits_attachments_key_when_none(monkeypatch):
    from uuid import uuid4

    captured = {}

    class _FakeGraphClient:
        def _request(self, method, path, json=None, retry_on=None, **_kw):
            captured["body"] = json

    @contextmanager
    def fake_with_outlook_client(control_db, tenant_db, user_id, tenant_id):
        yield _FakeGraphClient()

    import gdx_dispatch.modules.outlook.token_refresh as token_refresh
    monkeypatch.setattr(token_refresh, "with_outlook_client", fake_with_outlook_client)

    @contextmanager
    def fake_session_local():
        yield None

    import gdx_dispatch.core.database as core_db
    monkeypatch.setattr(core_db, "SessionLocal", fake_session_local)

    sent, reason = tx._try_outlook_graph(
        tenant_db=None, tenant_id=uuid4(), user_id=uuid4(),
        to_email="cust@example.com", subject="Invoice", html_body="<p>hi</p>",
    )

    assert (sent, reason) == (True, None)
    assert "attachments" not in captured["body"]["message"]


# ── graph_send routing and the unconfirmed-send stop (2026-10-16) ──────
#
# Transactional email used to POST /me/sendMail with every file inline, so a
# picked photo or two on top of the PDF pushed the request past Graph's 4 MB
# cap and the whole email was refused. It now goes through graph_send: a big
# set takes the draft route. And a 5xx or timeout on the call that sends is
# no longer "failed, try the next rung": the mail may have gone, so neither
# SMTP nor the designated sender is tried after it.

import httpx  # noqa: E402
import pytest  # noqa: E402

from gdx_dispatch.modules.outlook import graph_send  # noqa: E402
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError  # noqa: E402


class _Resp:
    def __init__(self, data=None):
        self._data = data or {}

    def json(self):
        return self._data


class _RecordingGraph:
    """Records every Graph call; ``fail_on`` maps a path suffix to the
    exception that call raises."""

    def __init__(self, fail_on=None):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on or {}

    def _request(self, method, path, json=None, **_kw):
        self.calls.append((method, path))
        for suffix, exc in self.fail_on.items():
            if path.endswith(suffix):
                raise exc
        if method == "POST" and path == "/me/messages":
            return _Resp({"id": "draft-1"})
        return _Resp()


def _patch_graph(monkeypatch, gc):
    from uuid import uuid4  # noqa: F401

    @contextmanager
    def fake_with_outlook_client(control_db, tenant_db, user_id, tenant_id):
        yield gc

    @contextmanager
    def fake_session_local():
        yield None

    import gdx_dispatch.core.database as core_db
    import gdx_dispatch.modules.outlook.token_refresh as token_refresh
    monkeypatch.setattr(token_refresh, "with_outlook_client", fake_with_outlook_client)
    monkeypatch.setattr(core_db, "SessionLocal", fake_session_local)


def _att(n_bytes: int, name="photo.jpg"):
    return {"name": name, "content_type": "image/jpeg",
            "content_base64": base64.b64encode(b"x" * n_bytes).decode("ascii"),
            "size_bytes": n_bytes}


def _graph(**kw):
    from uuid import uuid4
    return tx._try_outlook_graph(tenant_db=None, tenant_id=uuid4(), user_id=uuid4(),
                                 to_email="cust@example.com", subject="Invoice",
                                 html_body="<p>hi</p>", **kw)


def test_a_set_over_the_inline_cap_goes_through_a_draft(monkeypatch):
    gc = _RecordingGraph()
    _patch_graph(monkeypatch, gc)

    sent, reason = _graph(attachments=[PDF_ATT, _att(graph_send.INLINE_TOTAL_BYTES)])

    assert (sent, reason) == (True, None)
    assert gc.calls == [
        ("POST", "/me/messages"),
        ("POST", "/me/messages/draft-1/attachments"),
        ("POST", "/me/messages/draft-1/attachments"),
        ("POST", "/me/messages/draft-1/send"),
    ]


@pytest.mark.parametrize("failure", [
    OutlookGraphAPIError(503, "busy", endpoint="send"),
    httpx.ReadTimeout("slow"),
], ids=["5xx", "timeout"])
def test_an_unanswered_send_is_unconfirmed(monkeypatch, failure):
    _patch_graph(monkeypatch, _RecordingGraph(fail_on={"/send": failure}))
    sent, reason = _graph(attachments=[_att(graph_send.INLINE_TOTAL_BYTES + 1)])
    assert (sent, reason) == (False, tx.OUTLOOK_SEND_UNCONFIRMED)


def test_a_failed_attach_is_a_plain_failure_not_unconfirmed(monkeypatch):
    """Nothing was sent when an attach step fails, so the next rung may
    still be tried — only the sending call is ambiguous."""
    gc = _RecordingGraph(fail_on={"/attachments": OutlookGraphAPIError(503, "busy", endpoint="att")})
    _patch_graph(monkeypatch, gc)
    sent, reason = _graph(attachments=[_att(graph_send.INLINE_TOTAL_BYTES + 1)])
    assert sent is False and reason != tx.OUTLOOK_SEND_UNCONFIRMED
    assert ("POST", "/me/messages/draft-1/send") not in gc.calls
    assert ("DELETE", "/me/messages/draft-1") in gc.calls


def test_a_big_set_without_sent_items_is_refused_before_any_call(monkeypatch):
    gc = _RecordingGraph()
    _patch_graph(monkeypatch, gc)
    sent, reason = _graph(attachments=[_att(graph_send.INLINE_TOTAL_BYTES + 1)],
                          save_to_sent_items=False)
    assert (sent, reason) == (False, "attachments_need_sent_items")
    assert gc.calls == []


# ── send_transactional_email after an unconfirmed send ─────────────────

TENANT_UUID = "11111111-1111-1111-1111-111111111111"
CALLER_UUID = "22222222-2222-2222-2222-222222222222"
DESIGNATED_UUID = "33333333-3333-3333-3333-333333333333"


def _rungs(monkeypatch, outcomes):
    """Stub both rungs; ``outcomes`` maps a user id to its Graph result."""
    tried = {"graph": [], "smtp": 0}

    def fake_graph(**kw):
        tried["graph"].append(str(kw["user_id"]))
        return outcomes.get(str(kw["user_id"]), (False, "outlook_not_connected"))

    def fake_smtp(**kw):
        tried["smtp"] += 1
        return True, None

    monkeypatch.setattr(tx, "_try_outlook_graph", fake_graph)
    monkeypatch.setattr(tx, "_try_smtp", fake_smtp)
    return tried


def _send_tx(db, **kw):
    from uuid import uuid4
    return tx.send_transactional_email(
        tenant_db=db, tenant_id=TENANT_UUID, user_id=CALLER_UUID,
        to_email="cust@example.com", to_name="Cust", subject="Invoice",
        html_body="<p>hi</p>", kind="invoice", entity_type="invoice",
        entity_id=str(uuid4()), attachments=[PDF_ATT], **kw,
    )


def _last_row(db):
    from sqlalchemy import select

    from gdx_dispatch.models.tenant_models import OutboundEmail
    return db.execute(select(OutboundEmail).order_by(OutboundEmail.created_at.desc())).scalars().first()


def test_unconfirmed_never_falls_through_to_smtp(tenant_db, monkeypatch):
    tried = _rungs(monkeypatch, {CALLER_UUID: (False, tx.OUTLOOK_SEND_UNCONFIRMED)})

    result = _send_tx(tenant_db)

    assert result == (False, "outlook_graph", tx.OUTLOOK_SEND_UNCONFIRMED)
    assert tried == {"graph": [CALLER_UUID], "smtp": 0}
    row = _last_row(tenant_db)
    assert (row.status, row.provider, row.skip_reason) == (
        "failed", "outlook_graph", tx.OUTLOOK_SEND_UNCONFIRMED)


def test_unconfirmed_on_the_designated_sender_stops_there(tenant_db, monkeypatch):
    from gdx_dispatch.models.tenant_models import AppSettings
    tenant_db.add(AppSettings(automation_sender_user_id=DESIGNATED_UUID))
    tenant_db.commit()
    tried = _rungs(monkeypatch, {DESIGNATED_UUID: (False, tx.OUTLOOK_SEND_UNCONFIRMED)})

    result = _send_tx(tenant_db, designated_sender_fallback=True)

    assert result == (False, "outlook_graph", tx.OUTLOOK_SEND_UNCONFIRMED)
    assert tried == {"graph": [CALLER_UUID, DESIGNATED_UUID], "smtp": 0}


def test_a_plain_outlook_failure_still_falls_back_to_smtp(tenant_db, monkeypatch):
    """The stop is only for the ambiguous case; a refusal is unchanged."""
    tried = _rungs(monkeypatch, {CALLER_UUID: (False, "outlook_send_failed")})
    result = _send_tx(tenant_db)
    assert result[0] is True and tried["smtp"] == 1
