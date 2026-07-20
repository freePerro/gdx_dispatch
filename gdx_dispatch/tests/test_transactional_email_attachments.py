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
    assert parts[0].get_content_type() == "text/html"
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
        def _request(self, method, path, json=None):
            captured["method"] = method
            captured["path"] = path
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
        to_email="cust@example.com", subject="Invoice",
        html_body="<p>hi</p>", attachments=[PDF_ATT],
    )

    assert (sent, reason) == (True, None)
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
        def _request(self, method, path, json=None):
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
