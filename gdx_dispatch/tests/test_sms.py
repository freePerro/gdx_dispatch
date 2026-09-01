"""Unit tests for ``core/sms.py`` (Twilio sender).

Was ``test_email_sms.py`` until the removal of the Communications shell (#350):
``core/email.py`` went with that router — it was its only non-test importer —
so the email half of this file went too. ``core/sms.py`` stays: it has two
working consumers (``routers/voice.py`` missed-call auto-SMS and
``routers/dispatch_scheduling.py`` on-my-way).
"""
from __future__ import annotations

import logging
import types

from gdx_dispatch.core import sms


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
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)

    sms_result = sms.send_sms(
        to_phone="+15550001111",
        body="x",
        from_phone="+15550002222",
        tenant_id="tenant-2",
    )

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
