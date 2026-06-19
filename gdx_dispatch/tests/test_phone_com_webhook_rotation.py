"""P1.4 — webhook secret rotation: previous-secret grace window + key_storage
rotate/revert helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base, Tenant
from gdx_dispatch.modules.phone_com.key_storage import (
    get_or_create_webhook_secret,
    revert_webhook_secret,
    rotate_webhook_secret,
)
from gdx_dispatch.modules.phone_com.webhook_signing import verify_webhook_path


@pytest.fixture
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def control_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    sess = sm()
    tid = uuid4()
    sess.add(Tenant(id=tid, slug="t1", name="Test"))
    sess.commit()
    yield sess, tid
    sess.close()


def test_rotate_returns_new_secret_and_stages_old_in_prev(control_session, fernet_env):
    sess, tid = control_session
    old = get_or_create_webhook_secret(sess, tid)
    returned_old, new = rotate_webhook_secret(sess, tid)
    assert returned_old == old
    assert new != old
    assert len(new) > 20
    # New is the active secret now.
    assert verify_webhook_path(tid, new, sess) is True
    # Old still passes during the grace window.
    assert verify_webhook_path(tid, old, sess) is True


def test_rotate_grace_window_expires(control_session, fernet_env):
    sess, tid = control_session
    old = get_or_create_webhook_secret(sess, tid)
    rotate_webhook_secret(sess, tid, grace_seconds=1)
    # Force the prev_until into the past.
    from gdx_dispatch.control.models import TenantSettings
    s = sess.get(TenantSettings, tid)
    s.phone_com_webhook_secret_prev_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    sess.commit()
    assert verify_webhook_path(tid, old, sess) is False


def test_revert_restores_old_secret(control_session, fernet_env):
    sess, tid = control_session
    old = get_or_create_webhook_secret(sess, tid)
    _, new = rotate_webhook_secret(sess, tid)
    revert_webhook_secret(sess, tid)
    assert verify_webhook_path(tid, old, sess) is True
    assert verify_webhook_path(tid, new, sess) is False


def test_rotate_first_time_no_prior_secret(control_session, fernet_env):
    """Tenant with no current secret — rotation just produces a new one,
    no grace window (nothing to grace)."""
    sess, tid = control_session
    returned_old, new = rotate_webhook_secret(sess, tid)
    assert returned_old == ""
    assert new
    assert verify_webhook_path(tid, new, sess) is True


def test_clear_webhook_secret_clears_prev_too(control_session, fernet_env):
    from gdx_dispatch.modules.phone_com.key_storage import clear_webhook_secret
    sess, tid = control_session
    get_or_create_webhook_secret(sess, tid)
    rotate_webhook_secret(sess, tid)
    clear_webhook_secret(sess, tid)
    from gdx_dispatch.control.models import TenantSettings
    s = sess.get(TenantSettings, tid)
    assert s.phone_com_webhook_secret is None
    assert s.phone_com_webhook_secret_prev is None
    assert s.phone_com_webhook_secret_prev_until is None
