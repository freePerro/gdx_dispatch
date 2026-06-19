from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.core.pii import HashColumn
from gdx_dispatch.tasks import email_poller


class FakeIMAP:
    def __init__(self, raw_messages: dict[bytes, bytes]) -> None:
        self._raw_messages = raw_messages
        self.store_calls: list[tuple[bytes, str, str]] = []
        self.selected_mailbox: str | None = None

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        _ = (username, password)
        return ("OK", [b"logged-in"])

    def select(self, mailbox: str) -> tuple[str, list[bytes]]:
        self.selected_mailbox = mailbox
        return ("OK", [b"1"])

    def search(self, charset: str | None, criteria: str) -> tuple[str, list[bytes]]:
        _ = (charset, criteria)
        msg_ids = b" ".join(self._raw_messages.keys())
        return ("OK", [msg_ids])

    def fetch(self, msg_id: bytes, query: str) -> tuple[str, list[tuple[bytes, bytes]]]:
        _ = query
        return ("OK", [(b"RFC822", self._raw_messages[msg_id])])

    def store(self, msg_id: bytes, op: str, flag: str) -> tuple[str, list[bytes]]:
        self.store_calls.append((msg_id, op, flag))
        return ("OK", [b"stored"])

    def logout(self) -> tuple[str, list[bytes]]:
        return ("OK", [b"bye"])


@pytest.fixture()
def app_session_factory(tmp_path: Path) -> sessionmaker:
    """Single-tenant app DB for email poller tests."""
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE app_settings (id TEXT PRIMARY KEY, integrations JSON)"
        ))
        conn.execute(text(
            "CREATE TABLE customers (id TEXT PRIMARY KEY, email_hash TEXT NULL, company_id TEXT)"
        ))
        conn.execute(text(
            """CREATE TABLE inbound_emails (
                id TEXT PRIMARY KEY,
                customer_id TEXT NULL,
                message_id TEXT NOT NULL UNIQUE,
                sender_email TEXT NOT NULL,
                subject TEXT NULL,
                body_text TEXT NULL,
                created_at TEXT NOT NULL
            )"""
        ))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_imap_settings(db: Session, host: str = "imap.test.local") -> None:
    db.execute(
        text("INSERT INTO app_settings (id, integrations) VALUES ('s1', :integrations)"),
        {
            "integrations": (
                '{"imap": {"host": "'
                + host
                + '", "port": 993, "username": "user", "password": "pass", "mailbox": "INBOX"}}'
            )
        },
    )
    db.commit()


def _build_email_bytes(message_id: str, sender: str, subject: str = "Hello", body: str = "Body") -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def test_poll_creates_inbound_email_and_marks_read(
    monkeypatch: pytest.MonkeyPatch, app_session_factory: sessionmaker
) -> None:
    with app_session_factory() as db:
        _seed_imap_settings(db)

    fake_imap = FakeIMAP({b"1": _build_email_bytes("<m1@test>", "Alice <alice@example.com>")})
    monkeypatch.setattr(email_poller, "SessionLocal", app_session_factory)
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", lambda *_args, **_kwargs: fake_imap)

    out = email_poller.poll_inbound_emails()

    assert out["processed"] == 1
    assert fake_imap.store_calls == [(b"1", "+FLAGS", "\\Seen")]
    with app_session_factory() as db:
        row = db.execute(text("SELECT message_id, sender_email FROM inbound_emails")).mappings().one()
        assert row["message_id"] == "<m1@test>"
        assert row["sender_email"] == "alice@example.com"


def test_poll_matches_sender_to_customer(
    monkeypatch: pytest.MonkeyPatch, app_session_factory: sessionmaker
) -> None:
    with app_session_factory() as db:
        _seed_imap_settings(db)
        db.execute(
            text("INSERT INTO customers (id, email_hash, company_id) VALUES ('c1', :email_hash, 'tenant-test')"),
            {"email_hash": HashColumn.hash_for_search("alice@example.com")},
        )
        db.commit()

    fake_imap = FakeIMAP({b"7": _build_email_bytes("<m2@test>", "alice@example.com")})
    monkeypatch.setattr(email_poller, "SessionLocal", app_session_factory)
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", lambda *_args, **_kwargs: fake_imap)

    out = email_poller.poll_inbound_emails()

    assert out["processed"] == 1
    with app_session_factory() as db:
        row = db.execute(text("SELECT customer_id FROM inbound_emails WHERE message_id = '<m2@test>'")).mappings().one()
        assert row["customer_id"] == "c1"


def test_poll_skips_when_imap_config_missing(
    monkeypatch: pytest.MonkeyPatch, app_session_factory: sessionmaker
) -> None:
    with app_session_factory() as db:
        db.execute(text("INSERT INTO app_settings (id, integrations) VALUES ('s1', '{}')"))
        db.commit()

    monkeypatch.setattr(email_poller, "SessionLocal", app_session_factory)
    imap_ctor = MagicMock()
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", imap_ctor)

    out = email_poller.poll_inbound_emails()

    assert out["skipped"] == 1
    imap_ctor.assert_not_called()


def test_poll_skips_duplicate_message_id(
    monkeypatch: pytest.MonkeyPatch, app_session_factory: sessionmaker
) -> None:
    with app_session_factory() as db:
        _seed_imap_settings(db)
        db.execute(
            text(
                """
                INSERT INTO inbound_emails (id, customer_id, message_id, sender_email, subject, body_text, created_at)
                VALUES ('i1', NULL, '<dup@test>', 'alice@example.com', 'old', 'old', '2026-01-01T00:00:00Z')
                """
            )
        )
        db.commit()

    fake_imap = FakeIMAP({b"9": _build_email_bytes("<dup@test>", "alice@example.com")})
    monkeypatch.setattr(email_poller, "SessionLocal", app_session_factory)
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", lambda *_args, **_kwargs: fake_imap)

    out = email_poller.poll_inbound_emails()

    assert out["duplicates"] == 1
    with app_session_factory() as db:
        count = db.execute(text("SELECT COUNT(*) FROM inbound_emails WHERE message_id = '<dup@test>'")).scalar_one()
        assert count == 1


def test_poll_handles_imap_connection_failure(
    monkeypatch: pytest.MonkeyPatch, app_session_factory: sessionmaker
) -> None:
    with app_session_factory() as db:
        _seed_imap_settings(db)

    log_exception = MagicMock()
    monkeypatch.setattr(email_poller, "SessionLocal", app_session_factory)
    monkeypatch.setattr(email_poller, "log", SimpleNamespace(info=MagicMock(), exception=log_exception))
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", MagicMock(side_effect=OSError("imap down")))

    out = email_poller.poll_inbound_emails()

    assert out["errors"] == 1
    assert log_exception.called


def test_poll_logs_audit_events(
    monkeypatch: pytest.MonkeyPatch, app_session_factory: sessionmaker
) -> None:
    with app_session_factory() as db:
        _seed_imap_settings(db)

    fake_imap = FakeIMAP({b"2": _build_email_bytes("<m6@test>", "bob@example.com")})
    audit_mock = MagicMock()
    monkeypatch.setattr(email_poller, "SessionLocal", app_session_factory)
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", lambda *_args, **_kwargs: fake_imap)
    monkeypatch.setattr(email_poller, "log_audit_event_sync", audit_mock)

    out = email_poller.poll_inbound_emails()

    assert out["processed"] == 1
    assert audit_mock.call_count >= 2
