"""GDXA-689 — password-reset email goes through Outlook as the designated
"Send as" mailbox, with the live link kept out of that mailbox's Sent Items
and out of outbound_emails.

Before this, POST /auth/forgot-password only tried platform SMTP; on prod
PLATFORM_SMTP_USER is the placeholder account (compose passes it in), so the
login failed and the error was swallowed — no reset email reached anyone.

The Graph rung here is real down to the HTTP call: only
``with_outlook_client`` is stubbed, so the assertions read the exact JSON
that would be POSTed to /me/sendMail.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from gdx_dispatch.core import transactional_email as te
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import AppSettings, OutboundEmail, User
from gdx_dispatch.routers.auth import core as auth_core

TENANT = "11111111-1111-1111-1111-111111111111"
DESIGNATED = UUID("33333333-3333-3333-3333-333333333333")
GENERIC = "If an account exists with this email, a reset link has been sent."


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    def expire(self, key, ttl):
        return True

    def setex(self, key, ttl, value):
        self.store[key] = value

    def reset_tokens(self):
        return [k.split(":", 1)[1] for k in self.store if k.startswith("pw_reset:")]


@pytest.fixture()
def env(tenant_db, monkeypatch):
    monkeypatch.setenv("GDX_TENANT_ID", TENANT)
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.test")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "info@example.com")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "")
    fake_redis = _FakeRedis()
    monkeypatch.setattr(auth_core, "redis", fake_redis)

    user = User(
        id=uuid4(), email="owner@example.com", name="Owner", company_id=TENANT,
        role="admin", active=True,
    )
    tenant_db.add(user)
    tenant_db.commit()

    app = FastAPI()
    app.include_router(auth_core.router)
    app.dependency_overrides[get_db] = lambda: tenant_db
    return SimpleNamespace(
        db=tenant_db, client=TestClient(app), redis=fake_redis, user=user,
    )


def _designate(db, user_id):
    db.add(AppSettings(automation_sender_user_id=str(user_id) if user_id else None))
    db.commit()


def _graph(monkeypatch, *, connected: set[UUID]):
    """Stub only the Graph client; records (sender, method, path, json)."""
    import gdx_dispatch.core.database as database_mod
    import gdx_dispatch.modules.outlook.token_refresh as tr

    calls: list[tuple] = []

    @contextmanager
    def fake_client(control_db, tenant_db, user_id, tenant_id):
        if user_id not in connected:
            raise tr.OutlookReconnectRequired("user has not connected Outlook")
        yield SimpleNamespace(
            _request=lambda method, path, json=None, retry_on=None: calls.append((user_id, method, path, json)),
        )

    monkeypatch.setattr(tr, "with_outlook_client", fake_client)
    monkeypatch.setattr(database_mod, "SessionLocal", MagicMock())
    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (False, "smtp_not_configured"))
    return calls


def _platform(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        auth_core, "_send_platform_email", lambda to, subj, body: sent.append((to, subj, body)),
    )
    return sent


def _rows(db):
    return db.execute(select(OutboundEmail)).scalars().all()


def _post(env, email="owner@example.com"):
    r = env.client.post("/auth/forgot-password", json={"email": email})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": GENERIC}
    return r


def test_reset_goes_through_designated_sender_without_sent_items_or_stored_token(
    env, monkeypatch, caplog,
):
    _designate(env.db, DESIGNATED)
    calls = _graph(monkeypatch, connected={DESIGNATED})
    platform = _platform(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _post(env)

    [token] = env.redis.reset_tokens()
    assert len(calls) == 1
    sender, method, path, body = calls[0]
    assert (sender, method, path) == (DESIGNATED, "POST", "/me/sendMail")
    assert body["saveToSentItems"] is False
    assert body["message"]["toRecipients"] == [{"emailAddress": {"address": "owner@example.com"}}]
    # The customer-facing email carries the live link …
    assert f"/reset-password?token={token}" in body["message"]["body"]["content"]
    assert platform == []

    # … the audit copy does not.
    [row] = _rows(env.db)
    assert row.status == "sent" and row.provider == "outlook_graph"
    assert row.kind == "password_reset" and row.entity_type == "user"
    assert row.entity_id == str(env.user.id)
    assert row.to_email == "owner@example.com"
    assert row.subject == "Password Reset — DispatchApp"
    assert token not in row.body_html
    assert "reset-password?token=" not in row.body_html
    assert "[reset link redacted]" in row.body_html

    assert token not in caplog.text
    assert "reset-password?token=" not in caplog.text


def test_no_outlook_falls_back_to_platform_smtp_when_configured(env, monkeypatch):
    _designate(env.db, None)
    calls = _graph(monkeypatch, connected=set())
    platform = _platform(monkeypatch)
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "mailer@mailbox.test")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "secret")

    _post(env)

    [token] = env.redis.reset_tokens()
    assert calls == []
    assert len(platform) == 1
    to, subject, html = platform[0]
    assert to == "owner@example.com"
    assert f"/reset-password?token={token}" in html
    # Both attempts are on the trail, redacted: the transactional one that
    # failed, and the platform SMTP one that delivered.
    rows = sorted(_rows(env.db), key=lambda r: r.status)
    assert [(r.status, r.provider) for r in rows] == [("failed", None), ("sent", "platform_smtp")]
    assert all(token not in r.body_html and "[reset link redacted]" in r.body_html for r in rows)
    assert {(r.initiator_kind, r.initiator_ref) for r in rows} == {("system", None)}


def test_neither_provider_warns_without_address_or_token(env, monkeypatch, caplog):
    _designate(env.db, None)
    _graph(monkeypatch, connected=set())
    platform = _platform(monkeypatch)
    # Prod's shape: a password, and the placeholder account compose passes
    # in. Logging in as the placeholder must not be tried.
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "secret")

    with caplog.at_level(logging.WARNING, logger=auth_core.log.name):
        _post(env)

    [token] = env.redis.reset_tokens()
    assert platform == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
                and r.name == auth_core.log.name]
    assert warnings == ["password_reset_email_not_sent reason=no_email_provider_connected"]
    assert "owner@example.com" not in caplog.text
    assert token not in caplog.text


def test_designated_sender_whose_connection_is_dead_still_falls_back(env, monkeypatch):
    _designate(env.db, DESIGNATED)
    _graph(monkeypatch, connected=set())
    platform = _platform(monkeypatch)
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "mailer@mailbox.test")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "secret")

    _post(env)

    assert len(platform) == 1


@pytest.mark.parametrize("outlook", ["connected", "not_connected"])
@pytest.mark.parametrize("base", ["", "https://gdx.example.com", "https://a.test,https://b.test"])
def test_unusable_public_url_sends_nothing_and_stores_no_token(env, monkeypatch, caplog, base, outlook):
    # A link built on these is dead in the inbox; a token stored for it is a
    # live credential nobody received (GDXA-677). Outlook delivers first, so
    # platform SMTP is only reached — and only guarded — when it is not
    # connected; each param leaves one provider ready for the URL guard to stop.
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", base)
    _designate(env.db, DESIGNATED)
    calls = _graph(monkeypatch, connected={DESIGNATED} if outlook == "connected" else set())
    platform = _platform(monkeypatch)
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "mailer@mailbox.test")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "secret")

    with caplog.at_level(logging.WARNING, logger=auth_core.log.name):
        _post(env)

    assert calls == [] and platform == []
    assert env.redis.reset_tokens() == []
    assert "password_reset_email_not_sent reason=public_url_unusable" in caplog.text


def test_unknown_email_sends_nothing_and_answers_the_same(env, monkeypatch):
    _designate(env.db, DESIGNATED)
    calls = _graph(monkeypatch, connected={DESIGNATED})
    platform = _platform(monkeypatch)

    _post(env, email="nobody@example.com")

    assert calls == [] and platform == []
    assert env.redis.reset_tokens() == []
    assert _rows(env.db) == []


def test_invoice_style_send_is_unchanged(env, monkeypatch):
    """Defaults: Sent Items still on, body stored in full."""
    calls = _graph(monkeypatch, connected={DESIGNATED})
    body = "<p>Invoice 1001 — pay at https://gdx.example.com/pay/abc</p>"

    sent, provider, skip = te.send_transactional_email(
        tenant_db=env.db, tenant_id=TENANT, user_id=str(DESIGNATED),
        to_email="cust@example.com", to_name="Cust", subject="Invoice 1001",
        html_body=body, kind="invoice", entity_type="invoice", entity_id=str(uuid4()),
    )

    assert (sent, provider, skip) == (True, "outlook_graph", None)
    assert calls[0][3]["saveToSentItems"] is True
    [row] = _rows(env.db)
    assert row.body_html == body
