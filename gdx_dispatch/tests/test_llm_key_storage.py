"""Sprint 1.x-S4 — round-trip + audit + Fernet-failure tests for key_storage.

SQLite in-memory + control-plane Base.metadata is enough to exercise set/get/
clear; ``log_audit_event_sync`` is mocked because the audit hash-chain
infrastructure is its own integration target. The work-order's lab verify
steps run the real path end-to-end.
"""
from __future__ import annotations

import os
from unittest import mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base, Tenant, TenantSettings
from gdx_dispatch.core.llm.key_storage import (
    LLMKeyStorageError,
    clear_key,
    get_key,
    set_key,
    test_the_key,
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
    with mock.patch("gdx_dispatch.core.llm.key_storage.log_audit_event_sync") as m:
        yield m


def test_round_trip(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_key(sess, tid, "sk-ant-test-roundtrip")
    assert get_key(sess, tid) == "sk-ant-test-roundtrip"


def test_clear_resets_all_columns(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_key(sess, tid, "sk-ant-test")
    clear_key(sess, tid)

    assert get_key(sess, tid) is None
    settings = sess.get(TenantSettings, tid)
    assert settings is not None
    assert settings.llm_provider_key_enc is None
    assert settings.llm_provider_key_set_at is None
    assert settings.llm_provider_key_last_validated_at is None
    assert settings.llm_provider_key_last_error is None


def test_set_key_is_upsert(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_key(sess, tid, "first")
    set_key(sess, tid, "second")

    rows = sess.query(TenantSettings).filter_by(tenant_id=tid).all()
    assert len(rows) == 1
    assert get_key(sess, tid) == "second"


def test_missing_fernet_key_raises(session_with_tenant, monkeypatch, silent_audit):
    sess, tid = session_with_tenant
    monkeypatch.delenv("GDX_FERNET_KEY", raising=False)

    with pytest.raises(LLMKeyStorageError, match="GDX_FERNET_KEY not configured"):
        set_key(sess, tid, "anything")


def test_get_key_raises_on_decryption_failure(session_with_tenant, fernet_env, silent_audit, monkeypatch):
    sess, tid = session_with_tenant
    set_key(sess, tid, "sk-ant-test")
    # Rotate the Fernet key after writing — old ciphertext now undecryptable.
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())

    with pytest.raises(LLMKeyStorageError, match="cannot be decrypted"):
        get_key(sess, tid)


def test_audit_calls_made(session_with_tenant, fernet_env, silent_audit):
    sess, tid = session_with_tenant
    set_key(sess, tid, "k", user_id="admin-1")
    clear_key(sess, tid, user_id="admin-1")

    actions = [c.kwargs["action"] for c in silent_audit.call_args_list]
    assert actions == ["ai_settings.key_set", "ai_settings.key_cleared"]
    for c in silent_audit.call_args_list:
        assert c.kwargs["tenant_id"] == str(tid)
        assert c.kwargs["user_id"] == "admin-1"
        assert c.kwargs["entity_type"] == "tenant_settings"
        assert c.kwargs["entity_id"] == str(tid)


def test_test_the_key_no_key_set(session_with_tenant, silent_audit):
    sess, tid = session_with_tenant
    # No set_key called
    result = test_the_key(sess, tid)

    assert result["ok"] is False
    assert result["error"] == "no key set"
    
    # Check audit
    actions = [c.kwargs["action"] for c in silent_audit.call_args_list]
    assert "ai_settings.key_tested" in actions


def test_test_the_key_success(
    session_with_tenant, fernet_env, silent_audit, monkeypatch
):
    sess, tid = session_with_tenant
    set_key(sess, tid, "sk-ant-valid")

    mock_client = mock.Mock()
    monkeypatch.setattr("gdx_dispatch.core.llm.anthropic_client.get_client", lambda db, t: mock_client)

    result = test_the_key(sess, tid)

    assert result["ok"] is True
    assert result["model"] == "claude-haiku-4-5"
    assert result["error"] is None
    assert result["latency_ms"] >= 0

    settings = sess.get(TenantSettings, tid)
    assert settings.llm_provider_key_last_validated_at is not None
    assert settings.llm_provider_key_last_error is None

    # Check audit
    last_audit = silent_audit.call_args_list[-1]
    assert last_audit.kwargs["action"] == "ai_settings.key_tested"
    assert last_audit.kwargs["details"]["ok"] is True


def test_test_the_key_auth_error(
    session_with_tenant, fernet_env, silent_audit, monkeypatch
):
    sess, tid = session_with_tenant
    set_key(sess, tid, "sk-ant-invalid")

    mock_client = mock.Mock()
    # The implementation catches Exception broadly (Anthropic SDK exception
    # hierarchy + httpx transport errors); a plain Exception exercises the
    # same path without needing httpx.Response/Request scaffolding.
    mock_client.messages.create.side_effect = Exception("Invalid API Key")
    monkeypatch.setattr("gdx_dispatch.core.llm.anthropic_client.get_client", lambda db, t: mock_client)

    result = test_the_key(sess, tid)

    assert result["ok"] is False
    assert "Invalid API Key" in result["error"]

    settings = sess.get(TenantSettings, tid)
    assert settings.llm_provider_key_last_error == "Invalid API Key"
    # last_validated_at should remain None if it was never set
    assert settings.llm_provider_key_last_validated_at is None

    # Check audit
    last_audit = silent_audit.call_args_list[-1]
    assert last_audit.kwargs["action"] == "ai_settings.key_tested"
    assert last_audit.kwargs["details"]["ok"] is False
    assert last_audit.kwargs["details"]["error"] == "Invalid API Key"


def test_test_the_key_api_error(
    session_with_tenant, fernet_env, silent_audit, monkeypatch
):
    sess, tid = session_with_tenant
    set_key(sess, tid, "sk-ant-valid")

    mock_client = mock.Mock()
    mock_client.messages.create.side_effect = Exception("Something went wrong")
    monkeypatch.setattr("gdx_dispatch.core.llm.anthropic_client.get_client", lambda db, t: mock_client)

    result = test_the_key(sess, tid)

    assert result["ok"] is False
    assert result["error"] == "Something went wrong"

    settings = sess.get(TenantSettings, tid)
    assert settings.llm_provider_key_last_error == "Something went wrong"