"""GDXA-677 — Users → "Send Reset Link" and Add User → "Send invite" do the work.

Before: POST /api/users/{id}/send-reset-link minted a token it stored nowhere,
sent no email and answered {"ok": "reset_link_queued"} — the UI toasted
"Reset link sent". POST /api/users/invite created the user with a random
password nobody ever saw and logged "user_invite_email_queued" while sending
nothing, so the invitee had no way in.

These tests run the real users router and the real POST /auth/reset-password
on a fresh SQLite and a fresh Postgres database, each built from the ORM,
with an in-process Redis. No Outlook sender is designated, so delivery falls through GDXA-689's provider
order to platform SMTP, whose transport (_send_platform_email) is replaced by
a recorder; the link it captured is
then USED, so a pass means the emailed link actually sets the password.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import fakeredis
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from werkzeug.security import check_password_hash

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings, OutboundEmail, User
from gdx_dispatch.routers.auth import core as auth_core
from gdx_dispatch.routers.auth import get_current_user

TENANT = "67767767-7677-4677-8677-677677677677"
NEW_PASSWORD = "Fresh-Passw0rd-677"


@pytest.fixture(params=["sqlite", "postgres"])
def orm_engine(request):
    """A fresh database built by create_all from the ORM, on both dialects.

    SQLite runs since GDXA-723: users._get_user_or_404 and /auth/reset-password
    used to bind the str id against the Uuid column, which SQLite's bind
    rejects ('str' has no .hex), so this suite was Postgres-only. Prod is
    Postgres. Not pg_test_engine: its structure.sql predates users.shift_start,
    so a User insert fails.
    """
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite://", future=True,
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        TenantBase.metadata.create_all(engine)
        yield engine
        engine.dispose()
        return

    import psycopg2

    from gdx_dispatch.tests.fixtures import pg

    try:
        pg._admin_conn().close()
    except psycopg2.OperationalError as exc:
        pg._skip_unless_ci(f"PostgreSQL not reachable at {pg.PG_HOST}:{pg.PG_PORT}: {exc}")
    name = f"gdx_test_{uuid4().hex[:12]}"
    pg._create_db(name)
    engine = create_engine(
        f"postgresql+psycopg2://{pg.PG_USER}:{pg.PG_PASSWORD}@{pg.PG_HOST}:{pg.PG_PORT}/{name}",
        future=True,
    )
    try:
        TenantBase.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        pg._drop_db(name)


@pytest.fixture()
def env(monkeypatch, orm_engine):
    from gdx_dispatch.routers import users as users_mod

    SessionLocal = sessionmaker(bind=orm_engine, autoflush=False, autocommit=False)

    admin_id = uuid4()
    target_id = uuid4()
    with SessionLocal() as db:
        db.add(User(id=admin_id, username="Pat Admin", email="pat.admin@example.com",
                    role="admin", company_id=TENANT, active=True))
        db.add(User(id=target_id, username="Terry Tech", email="terry.tech@example.com",
                    role="technician", company_id=TENANT, active=True,
                    password_hash="old-hash"))
        db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(users_mod.router)
    app.include_router(auth_core.router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    login = {"user_id": str(admin_id), "sub": str(admin_id), "tenant_id": TENANT, "role": "admin"}
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: dict(login)
    app.dependency_overrides[require_module("jobs")] = lambda: None

    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(auth_core, "redis", fake_redis)
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "accounts@mailbox.test")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "pw")
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.test/")
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(auth_core, "_send_platform_email", lambda *a: sent.append(a))

    client = TestClient(app, raise_server_exceptions=True)
    yield {
        "client": client, "db": SessionLocal, "redis": fake_redis, "sent": sent,
        "target_id": str(target_id), "admin_id": str(admin_id),
    }
    app.dependency_overrides.clear()


def _token_from(html: str) -> str:
    # Absolute, on the configured host: a relative link is dead in a mail client.
    m = re.search(r'href="https://gdx\.test/reset-password\?token=([A-Za-z0-9_\-]+)"', html)
    assert m, "the email carries no absolute reset link"
    return m.group(1)


def _audit(SessionLocal, action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all())


def _pw_reset_keys(r) -> list[str]:
    return [k for k in r.scan_iter("pw_reset:*")]


def test_send_reset_link_emails_a_link_that_resets_the_password(env, caplog):
    client, sent = env["client"], env["sent"]

    with caplog.at_level(logging.INFO, logger="gdx_dispatch.routers.users"):
        r = client.post(f"/api/users/{env['target_id']}/send-reset-link")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": "reset_link_sent", "email": "terry.tech@example.com"}

    assert len(sent) == 1
    to, subject, html = sent[0]
    assert to == "terry.tech@example.com"
    assert "Password Reset" in subject
    token = _token_from(html)
    assert env["redis"].ttl(f"pw_reset:{token}") <= auth_core.PASSWORD_RESET_TTL_SECONDS

    # The link works: the same token sets the password through the real endpoint.
    r2 = client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert r2.status_code == 200, r2.text
    with env["db"]() as db:
        u = db.get(User, UUID(env["target_id"]))
        assert check_password_hash(u.password_hash, NEW_PASSWORD)

    rows = _audit(env["db"], "password_reset_link_sent")
    assert len(rows) == 1
    assert rows[0].entity_id == env["target_id"]
    assert "token_prefix" not in rows[0].details
    assert token[:8] not in json.dumps(rows[0].details), "no part of the live credential in this audit row"

    # CodeQL py/clear-text-logging-sensitive-data: no part of the token in the log line.
    sent_logs = [rec for rec in caplog.records if rec.getMessage() == "password_reset_link_sent"]
    assert len(sent_logs) == 1
    assert not hasattr(sent_logs[0], "token_prefix")
    assert token[:8] not in json.dumps(sent_logs[0].__dict__, default=str)


def test_send_reset_link_goes_through_the_designated_outlook_sender(env, monkeypatch):
    """Prod's path: platform SMTP is the placeholder, the Settings → Automation
    email "Send as" mailbox is connected. Only the Graph client is stubbed."""
    import gdx_dispatch.core.database as database_mod
    import gdx_dispatch.modules.outlook.token_refresh as tr
    from gdx_dispatch.core import transactional_email as te

    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "info@example.com")
    sender = uuid4()
    with env["db"]() as db:
        db.add(AppSettings(automation_sender_user_id=str(sender)))
        db.commit()
    calls: list[tuple] = []

    @contextmanager
    def fake_client(control_db, tenant_db, user_id, tenant_id):
        assert user_id == sender
        yield SimpleNamespace(_request=lambda method, path, json=None, retry_on=None: calls.append((method, path, json)))

    monkeypatch.setattr(tr, "with_outlook_client", fake_client)
    monkeypatch.setattr(database_mod, "SessionLocal", MagicMock())
    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (False, "smtp_not_configured"))

    r = env["client"].post(f"/api/users/{env['target_id']}/send-reset-link")
    assert r.status_code == 200, r.text
    assert env["sent"] == [], "platform SMTP is not tried once Outlook delivered"
    [(method, path, body)] = calls
    assert (method, path) == ("POST", "/me/sendMail")
    assert body["saveToSentItems"] is False
    token = _token_from(body["message"]["body"]["content"])

    with env["db"]() as db:
        [row] = db.execute(select(OutboundEmail)).scalars().all()
        assert row.status == "sent" and row.kind == "password_reset"
        # Who asked for it, not the mailbox that carried it (the designated sender).
        assert (row.initiator_kind, row.initiator_ref) == ("user", env["admin_id"])
        assert token not in row.body_html and "[reset link redacted]" in row.body_html

    r2 = env["client"].post("/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert r2.status_code == 200, r2.text


def test_send_reset_link_with_placeholder_smtp_user_refuses_and_leaves_no_token(env, monkeypatch):
    # Prod's state at filing: password set, user at the @example.com default.
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "info@example.com")

    r = env["client"].post(f"/api/users/{env['target_id']}/send-reset-link")
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert '"Send as" mailbox' in detail
    assert "no_email_provider_connected" not in detail, "provider reason codes stay in the log"
    assert env["sent"] == []
    assert _pw_reset_keys(env["redis"]) == []
    assert _audit(env["db"], "password_reset_link_sent") == [], "no audit row claiming a send"
    # The refusal raises before the users.py audit row, so this is the record
    # of who tried.
    with env["db"]() as db:
        [row] = db.execute(select(OutboundEmail)).scalars().all()
    assert row.status == "failed"
    assert (row.initiator_kind, row.initiator_ref) == ("user", env["admin_id"])


def test_send_reset_link_smtp_failure_is_a_503_and_revokes_the_token(env, monkeypatch):
    def _boom(*_a):
        raise OSError("535 authentication failed")

    monkeypatch.setattr(auth_core, "_send_platform_email", _boom)

    r = env["client"].post(f"/api/users/{env['target_id']}/send-reset-link")
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "set up but sending failed" in detail, "a broken credential is not missing setup"
    assert "platform_smtp_failed" not in detail, "provider reason codes stay in the log"
    assert _pw_reset_keys(env["redis"]) == [], "a link nobody received must not stay live"
    assert _audit(env["db"], "password_reset_link_sent") == []
    with env["db"]() as db:
        rows = db.execute(select(OutboundEmail)).scalars().all()
    assert sorted(((r.status, r.provider, r.skip_reason) for r in rows), key=lambda t: t[1] or "") == [
        ("failed", None, "no_email_provider_connected"),
        ("failed", "platform_smtp", "platform_smtp_failed"),
    ]


@pytest.mark.parametrize(("skip_reason", "expected"), [
    ("outlook_reconnect_required", "needs to be reconnected"),
    ("outlook_send_failed", "set up but sending failed"),
    ("smtp_send_failed", "set up but sending failed"),
    ("smtp_exception", "set up but sending failed"),
    ("no_email_provider_connected", '"Send as" mailbox, or configure platform email'),
])
def test_send_reset_link_names_the_failure_the_provider_reported(env, monkeypatch, skip_reason, expected):
    # Prod sends through Outlook: an expired token must read as "reconnect",
    # not as "connect" — Settings already shows that mailbox as connected.
    from gdx_dispatch.core import transactional_email as te

    monkeypatch.setattr(te, "send_transactional_email", lambda **kw: (False, None, skip_reason))
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "")

    r = env["client"].post(f"/api/users/{env['target_id']}/send-reset-link")
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert expected in detail
    assert skip_reason not in detail, "provider reason codes stay in the log"
    assert _pw_reset_keys(env["redis"]) == []


@pytest.mark.parametrize("skip_reason", [
    "outlook_reconnect_required+platform_smtp_failed",
    "no_email_provider_connected+platform_smtp_failed",
])
def test_password_reset_not_sent_code_reads_every_joined_reason(skip_reason):
    expected = "outlook_reconnect" if skip_reason.startswith("outlook_reconnect") else "send_failed"
    assert auth_core._password_reset_not_sent_code(skip_reason) == expected


@pytest.mark.parametrize("base", [
    "", "https://a.test,https://b.test", "gdx.test",
    "https://gdx.example.com",  # .env.template's value: an absolute link to nowhere
])
def test_send_reset_link_without_a_usable_public_url_refuses(env, monkeypatch, base):
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", base)

    r = env["client"].post(f"/api/users/{env['target_id']}/send-reset-link")
    assert r.status_code == 503, r.text
    assert env["sent"] == []
    assert _pw_reset_keys(env["redis"]) == []
    assert _audit(env["db"], "password_reset_link_sent") == []


def test_a_dropped_quit_after_sendmail_still_counts_as_sent(monkeypatch):
    import smtplib

    delivered: list[str] = []

    class _Smtp:
        def __init__(self, *a, **k): ...
        def login(self, *a): ...
        def sendmail(self, _frm, to, _msg):
            delivered.append(to)
        def quit(self):
            raise smtplib.SMTPServerDisconnected("dropped on QUIT")

    monkeypatch.setattr(smtplib, "SMTP_SSL", _Smtp)
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "pw")
    auth_core._send_platform_email("x@example.com", "s", "<p>b</p>")  # must not raise
    assert delivered == ["x@example.com"]


def test_invite_emails_a_set_password_link_that_works(env):
    client, sent = env["client"], env["sent"]

    r = client.post("/api/users/invite", json={
        "email": "New.Hire@example.com", "name": "New Hire", "role": "technician",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invite_email_sent"] is True
    assert body["invite_error"] is None

    assert len(sent) == 1
    to, subject, html = sent[0]
    assert to == "new.hire@example.com"
    assert "invited" in subject.lower()
    token = _token_from(html)
    ttl = env["redis"].ttl(f"pw_reset:{token}")
    assert auth_core.PASSWORD_RESET_TTL_SECONDS < ttl <= auth_core.INVITE_RESET_TTL_SECONDS

    r2 = client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert r2.status_code == 200, r2.text
    with env["db"]() as db:
        u = db.execute(select(User).where(User.email == "new.hire@example.com")).scalar_one()
        assert check_password_hash(u.password_hash, NEW_PASSWORD)

    rows = _audit(env["db"], "user_invited")
    assert len(rows) == 1 and rows[0].details["invite_email_sent"] is True

    # No Outlook here, so platform SMTP delivered it: that attempt is a sent
    # row of its own, and both rows name the admin who sent the invite.
    with env["db"]() as db:
        out = db.execute(select(OutboundEmail)).scalars().all()
    assert sorted((o.status, o.provider) for o in out) == [("failed", None), ("sent", "platform_smtp")]
    assert {(o.initiator_kind, o.initiator_ref) for o in out} == {("user", env["admin_id"])}


def test_forgot_password_is_recorded_as_a_system_send(env):
    """Nobody is signed in on forgot-password, so no staff user is named."""
    r = env["client"].post("/auth/forgot-password", json={"email": "terry.tech@example.com"})
    assert r.status_code == 200, r.text
    assert len(env["sent"]) == 1
    with env["db"]() as db:
        out = db.execute(select(OutboundEmail)).scalars().all()
    assert sorted((o.status, o.provider) for o in out) == [("failed", None), ("sent", "platform_smtp")]
    assert {(o.initiator_kind, o.initiator_ref) for o in out} == {("system", None)}


def test_invite_without_smtp_creates_the_user_and_says_no_email_went(env, monkeypatch):
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "")

    r = env["client"].post("/api/users/invite", json={
        "email": "offline.hire@example.com", "name": "Offline Hire", "role": "technician",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invite_email_sent"] is False
    assert '"Send as" mailbox' in body["invite_error"]
    assert "no_email_provider_connected" not in body["invite_error"], "provider reason codes stay in the log"
    assert env["sent"] == []
    assert _pw_reset_keys(env["redis"]) == []

    with env["db"]() as db:
        assert db.execute(
            select(User).where(User.email == "offline.hire@example.com")
        ).scalar_one_or_none() is not None, "the user row exists either way"
    rows = _audit(env["db"], "user_invited")
    assert len(rows) == 1 and rows[0].details["invite_email_sent"] is False


@pytest.mark.parametrize(
    ("user", "password", "expected"),
    [
        ("accounts@mailbox.test", "pw", True),
        ("info@example.com", "pw", False),
        ("accounts@mailbox.test", "", False),
        ("", "pw", False),
    ],
)
def test_platform_smtp_configured(monkeypatch, user, password, expected):
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", user)
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", password)
    assert auth_core.platform_smtp_configured() is expected
