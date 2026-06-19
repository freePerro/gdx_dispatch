from __future__ import annotations

import logging
import types

from gdx_dispatch.core import email, sms


def test_send_email_smtp(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("MAIL_SERVER", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USERNAME", "mailer")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")

    calls: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def starttls(self) -> None:
            calls["tls"] = True

        def login(self, username: str, password: str) -> None:
            calls["username"] = username
            calls["password"] = password

        def send_message(self, message) -> None:
            calls["to"] = message["To"]
            calls["from"] = message["From"]
            calls["subject"] = message["Subject"]

    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)

    result = email.send_email(
        to="customer@example.com",
        subject="Welcome",
        body_html="<p>Hi</p>",
        from_address="no-reply@example.com",
        tenant_branding={"tenant": "GDX"},
    )

    assert result["sent"] is True
    assert result["provider"] == "smtp"
    assert calls["host"] == "smtp.example.com"
    assert calls["port"] == 587
    assert calls["to"] == "customer@example.com"
    assert calls["from"] == "no-reply@example.com"
    assert calls["subject"] == "Welcome"


def test_send_email_ses(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "ses")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY", "abc123")

    calls: dict[str, object] = {}

    class FakeSESClient:
        def send_email(self, **kwargs):
            calls["kwargs"] = kwargs
            return {"MessageId": "ses-123"}

    class FakeBoto3:
        def client(self, service_name: str, **kwargs):
            calls["service_name"] = service_name
            calls["client_kwargs"] = kwargs
            return FakeSESClient()

    monkeypatch.setattr(email, "_import_boto3", lambda: FakeBoto3())

    result = email.send_email(
        to="customer@example.com",
        subject="Invoice",
        body_html="<h1>Invoice</h1>",
        from_address="billing@example.com",
        tenant_branding={"brand": "GDX"},
    )

    assert result["sent"] is True
    assert result["provider"] == "ses"
    assert result["message_id"] == "ses-123"
    assert calls["service_name"] == "ses"
    assert calls["client_kwargs"]["region_name"] == "us-east-1"
    assert calls["client_kwargs"]["aws_access_key_id"] == "abc123"
    destination = calls["kwargs"]["Destination"]
    assert destination["ToAddresses"] == ["customer@example.com"]


def test_send_sms(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")

    calls: dict[str, object] = {}

    class FakeMessages:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs
            return types.SimpleNamespace(sid="SM123", status="queued")

    class FakeTwilioClient:
        def __init__(self, sid: str, token: str) -> None:
            calls["sid"] = sid
            calls["token"] = token
            self.messages = FakeMessages()

    monkeypatch.setattr(sms, "_import_twilio_client", lambda: FakeTwilioClient)

    result = sms.send_sms(
        to_phone="+15551112222",
        body="Your tech is on the way",
        from_phone="+15553334444",
        tenant_id="tenant-1",
    )

    assert result["sent"] is True
    assert result["provider"] == "twilio"
    assert result["message_id"] == "SM123"
    assert calls["sid"] == "AC123"
    assert calls["kwargs"]["to"] == "+15551112222"
    assert calls["kwargs"]["from_"] == "+15553334444"


def test_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)

    email_result = email.send_email(
        to="c@example.com",
        subject="x",
        body_html="<p>x</p>",
        from_address="noreply@example.com",
        tenant_branding={},
    )
    sms_result = sms.send_sms(
        to_phone="+15550001111",
        body="x",
        from_phone="+15550002222",
        tenant_id="tenant-2",
    )

    assert email_result == {"sent": False, "reason": "not configured"}
    assert sms_result == {"sent": False, "reason": "not configured"}


def test_sms_logged(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(sid="SM999", status="queued")

    class FakeTwilioClient:
        def __init__(self, sid: str, token: str) -> None:
            self.messages = FakeMessages()

    monkeypatch.setattr(sms, "_import_twilio_client", lambda: FakeTwilioClient)

    with caplog.at_level(logging.INFO):
        result = sms.send_sms(
            to_phone="+15559998888",
            body="Reminder",
            from_phone="+15558887777",
            tenant_id="tenant-42",
        )

    assert result["sent"] is True
    assert any(
        "tenant_id=tenant-42" in record.getMessage() and "sms" in record.getMessage().lower()
        for record in caplog.records
    )
