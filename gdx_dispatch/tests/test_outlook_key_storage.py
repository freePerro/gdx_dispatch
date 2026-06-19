"""Slice outlook-s5 — verify Fernet round-trips for tenant + per-user layers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base as ControlBase, Tenant, TenantSettings
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.outlook import key_storage
from gdx_dispatch.modules.outlook.models import OutlookAccount


@pytest.fixture
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def control_session():
    engine = create_engine("sqlite:///:memory:")
    ControlBase.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    sess = sm()
    tid = uuid4()
    sess.add(Tenant(id=tid, slug="t1", name="Test"))
    sess.commit()
    yield sess, tid
    sess.close()


@pytest.fixture
def tenant_session():
    engine = create_engine("sqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    sess = sm()
    # User.id in tenant_models is String(36) — match that for the test fixture.
    yield sess, str(uuid4())
    sess.close()


# ── tenant-level client_secret ──────────────────────────────────────────


def test_set_then_get_client_secret_round_trips(control_session, fernet_env):
    sess, tid = control_session
    key_storage.set_client_secret(sess, tid, "super-secret-from-azure")
    sess.commit()
    assert key_storage.get_client_secret(sess, tid) == "super-secret-from-azure"


def test_get_client_secret_returns_none_when_unset(control_session, fernet_env):
    sess, tid = control_session
    assert key_storage.get_client_secret(sess, tid) is None


def test_set_client_secret_stamps_set_at(control_session, fernet_env):
    sess, tid = control_session
    key_storage.set_client_secret(sess, tid, "abc")
    sess.commit()
    settings = sess.get(TenantSettings, tid)
    assert settings.outlook_secret_set_at is not None


def test_clear_client_secret_wipes_both_columns(control_session, fernet_env):
    sess, tid = control_session
    key_storage.set_client_secret(sess, tid, "abc")
    sess.commit()
    key_storage.clear_client_secret(sess, tid)
    sess.commit()
    settings = sess.get(TenantSettings, tid)
    assert settings.outlook_client_secret_enc is None
    assert settings.outlook_secret_set_at is None


# ── per-user tokens ─────────────────────────────────────────────────────


def test_set_then_get_user_tokens_round_trips(tenant_session, fernet_env):
    sess, uid = tenant_session
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    key_storage.set_user_tokens(
        sess, uid,
        access_token="acc-tok-1",
        refresh_token="ref-tok-1",
        access_token_expires_at=expires,
        upn="alice@gdx",
        display_name="Alice",
        scopes="Mail.Read offline_access",
    )
    sess.commit()
    got = key_storage.get_user_tokens(sess, uid)
    assert got is not None
    access, refresh, exp = got
    assert access == "acc-tok-1"
    assert refresh == "ref-tok-1"


def test_set_user_tokens_upserts_on_repeat(tenant_session, fernet_env):
    sess, uid = tenant_session
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    key_storage.set_user_tokens(sess, uid, access_token="a1", refresh_token="r1",
                                 access_token_expires_at=expires)
    key_storage.set_user_tokens(sess, uid, access_token="a2", refresh_token="r2",
                                 access_token_expires_at=expires)
    sess.commit()
    rows = sess.query(OutlookAccount).filter(OutlookAccount.user_id == uid).all()
    assert len(rows) == 1, "must upsert, not insert duplicates"
    got = key_storage.get_user_tokens(sess, uid)
    assert got[0] == "a2"


def test_get_user_tokens_returns_none_when_disconnected(tenant_session, fernet_env):
    sess, uid = tenant_session
    assert key_storage.get_user_tokens(sess, uid) is None


def test_clear_user_tokens_keeps_account_row(tenant_session, fernet_env):
    sess, uid = tenant_session
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    key_storage.set_user_tokens(sess, uid, access_token="a1", refresh_token="r1",
                                 access_token_expires_at=expires, upn="alice@gdx")
    sess.commit()
    key_storage.clear_user_tokens(sess, uid)
    sess.commit()
    account = sess.query(OutlookAccount).filter(OutlookAccount.user_id == uid).one()
    assert account.access_token_enc is None
    assert account.upn == "alice@gdx", "row + historical metadata must survive disconnect"


def test_no_fernet_key_raises_typed_error(monkeypatch, control_session):
    monkeypatch.delenv("GDX_FERNET_KEY", raising=False)
    sess, tid = control_session
    with pytest.raises(key_storage.OutlookKeyStorageError, match="GDX_FERNET_KEY"):
        key_storage.set_client_secret(sess, tid, "abc")


def test_invalid_fernet_key_raises_typed_error(monkeypatch, control_session):
    monkeypatch.setenv("GDX_FERNET_KEY", "not-a-real-fernet-key")
    sess, tid = control_session
    with pytest.raises(key_storage.OutlookKeyStorageError, match="not a valid Fernet key"):
        key_storage.set_client_secret(sess, tid, "abc")
