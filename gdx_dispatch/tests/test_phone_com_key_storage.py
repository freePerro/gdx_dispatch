"""Sprint 1.x — round-trip + audit + Fernet-failure tests for phone_com key_storage.

Mirrors ``test_llm_key_storage.py``. Exercises ``set_token`` / ``get_token``
/ ``clear_token`` / ``mark_validated`` / ``mark_failed`` plus the
webhook-secret helpers. No live Phone.com call here — that lands with
``client.py`` in a follow-up slice.
"""
from __future__ import annotations

from unittest import mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base, Tenant, TenantSettings
from gdx_dispatch.modules.phone_com.key_storage import (
    PhoneComKeyStorageError,
    clear_token,
    clear_webhook_secret,
    get_or_create_webhook_secret,
    get_token,
    mark_failed,
    mark_validated,
    set_token,
)


@pytest.fixture
def session_with_tenant():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionMaker()

    tenant_id = uuid4()
    sess.add(Tenant(id=tenant_id, slug="t1", name="Test"))
    sess.commit()
    yield sess, tenant_id
    sess.close()


@pytest.fixture
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def silent_audit():
    with mock.patch("gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync") as m:
        yield m


def test_round_trip(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "phc-permanent-test")
    assert get_token(sess, tid) == "phc-permanent-test"


def test_clear_resets_token_columns(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "phc-test")
    clear_token(sess, tid)

    assert get_token(sess, tid) is None
    settings = sess.get(TenantSettings, tid)
    assert settings is not None
    assert settings.phone_com_token_enc is None
    assert settings.phone_com_token_set_at is None
    assert settings.phone_com_token_last_validated_at is None
    assert settings.phone_com_token_last_error is None


def test_clear_token_preserves_webhook_secret(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "phc")
    secret = get_or_create_webhook_secret(sess, tid)
    clear_token(sess, tid)

    settings = sess.get(TenantSettings, tid)
    assert settings.phone_com_token_enc is None
    # Webhook secret intentionally survives token rotation.
    assert settings.phone_com_webhook_secret is not None
    assert get_or_create_webhook_secret(sess, tid) == secret


def test_set_token_is_upsert(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "first")
    set_token(sess, tid, "second")

    rows = sess.query(TenantSettings).filter_by(tenant_id=tid).all()
    assert len(rows) == 1
    assert get_token(sess, tid) == "second"


def test_missing_fernet_key_raises(session_with_tenant, monkeypatch, silent_audit):
    sess, tid = session_with_tenant
    monkeypatch.delenv("GDX_FERNET_KEY", raising=False)

    with pytest.raises(PhoneComKeyStorageError, match="GDX_FERNET_KEY not configured"):
        set_token(sess, tid, "anything")


def test_get_token_raises_on_decryption_failure(
    session_with_tenant, fernet_env, silent_audit, monkeypatch
):
    sess, tid = session_with_tenant
    set_token(sess, tid, "phc-test")
    # Rotate the Fernet key after writing — old ciphertext now undecryptable.
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())

    with pytest.raises(PhoneComKeyStorageError, match="cannot be decrypted"):
        get_token(sess, tid)


def test_audit_calls_made(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "k", user_id="admin-1")
    clear_token(sess, tid, user_id="admin-1")

    actions = [c.kwargs["action"] for c in silent_audit.call_args_list]
    assert actions == ["phone_com.token_set", "phone_com.token_cleared"]
    for c in silent_audit.call_args_list:
        assert c.kwargs["tenant_id"] == str(tid)
        assert c.kwargs["user_id"] == "admin-1"
        assert c.kwargs["entity_type"] == "tenant_settings"
        assert c.kwargs["entity_id"] == str(tid)


def test_mark_validated_then_failed_then_validated(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "phc")

    mark_failed(sess, tid, "401 unauthorized")
    settings = sess.get(TenantSettings, tid)
    assert settings.phone_com_token_last_error == "401 unauthorized"
    assert settings.phone_com_token_last_validated_at is None

    mark_validated(sess, tid)
    sess.refresh(settings)
    assert settings.phone_com_token_last_error is None
    assert settings.phone_com_token_last_validated_at is not None

    last_validated = settings.phone_com_token_last_validated_at
    mark_failed(sess, tid, "transient")
    sess.refresh(settings)
    # Last-known-good preserved on subsequent failure.
    assert settings.phone_com_token_last_validated_at == last_validated
    assert settings.phone_com_token_last_error == "transient"


def test_mark_failed_truncates_long_error(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_token(sess, tid, "phc")
    mark_failed(sess, tid, "x" * 1000)
    settings = sess.get(TenantSettings, tid)
    assert len(settings.phone_com_token_last_error) == 500


def test_webhook_secret_is_idempotent(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    a = get_or_create_webhook_secret(sess, tid)
    b = get_or_create_webhook_secret(sess, tid)
    assert a == b
    # token_urlsafe(32) returns ~43 chars after b64.
    assert len(a) >= 32


def test_clear_webhook_secret_then_regenerate(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    a = get_or_create_webhook_secret(sess, tid)
    clear_webhook_secret(sess, tid)
    b = get_or_create_webhook_secret(sess, tid)
    assert a != b


def test_set_token_strips_whitespace(session_with_tenant, fernet_env, silent_audit):
    """Copy-paste from the Phone.com console regularly drags trailing whitespace.
    Phone.com rejects 'Bearer token \\n' with 401 — strip on store + on read."""
    sess, tid = session_with_tenant
    set_token(sess, tid, "  phc-token-with-spaces  \n")
    assert get_token(sess, tid) == "phc-token-with-spaces"


def test_set_token_rejects_whitespace_only(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    import pytest as _pt
    with _pt.raises(Exception, match="empty"):
        set_token(sess, tid, "   \n  ")
