"""Sprint phone-com pc-s7 — URL-path-secret + voip_id payload verifier."""
from __future__ import annotations

from unittest import mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base, Tenant
from gdx_dispatch.modules.phone_com.key_storage import get_or_create_webhook_secret
from gdx_dispatch.modules.phone_com.webhook_signing import (
    WebhookAuthDecision,
    decide,
    verify_payload_voip_id,
    verify_webhook_path,
)


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


# ---------- verify_webhook_path ----------

def test_verify_webhook_path_match(control_session, fernet_env):
    sess, tid = control_session
    secret = get_or_create_webhook_secret(sess, tid)
    assert verify_webhook_path(tid, secret, sess) is True


def test_verify_webhook_path_mismatch_returns_false_not_raise(control_session, fernet_env):
    sess, tid = control_session
    get_or_create_webhook_secret(sess, tid)
    assert verify_webhook_path(tid, "wrong-secret", sess) is False


def test_verify_webhook_path_no_settings_row_returns_false(control_session, fernet_env):
    sess, _ = control_session
    foreign_tid = uuid4()
    assert verify_webhook_path(foreign_tid, "anything", sess) is False


def test_verify_webhook_path_uses_constant_time_compare(control_session, fernet_env):
    sess, tid = control_session
    secret = get_or_create_webhook_secret(sess, tid)
    import secrets as _secrets
    target = "gdx_dispatch.modules.phone_com.webhook_signing.secrets.compare_digest"
    with mock.patch(target, wraps=_secrets.compare_digest) as cd:
        verify_webhook_path(tid, secret, sess)
    assert cd.call_count >= 1, "constant-time compare must be used"


# ---------- verify_payload_voip_id ----------

def test_verify_payload_voip_id_match():
    assert verify_payload_voip_id({"voip_id": 1000000}, 1000000) is True


def test_verify_payload_voip_id_mismatch():
    assert verify_payload_voip_id({"voip_id": 99}, 1000000) is False


def test_verify_payload_voip_id_string_coerces_to_int():
    assert verify_payload_voip_id({"voip_id": "1000000"}, 1000000) is True


def test_verify_payload_voip_id_missing_returns_false():
    assert verify_payload_voip_id({"other": "data"}, 1000000) is False


# ---------- decide() pipeline ----------

def test_decide_path_mismatch_returns_404(control_session, fernet_env):
    sess, tid = control_session
    get_or_create_webhook_secret(sess, tid)
    d = decide(tid, "wrong-path", {"voip_id": 1000000}, sess, expected_voip_id=1000000)
    assert isinstance(d, WebhookAuthDecision)
    assert d.accepted is False
    assert d.status_code == 404
    assert "path" in d.reason.lower()


def test_decide_voip_id_mismatch_returns_400(control_session, fernet_env):
    sess, tid = control_session
    secret = get_or_create_webhook_secret(sess, tid)
    d = decide(tid, secret, {"voip_id": 99}, sess, expected_voip_id=1000000)
    assert d.accepted is False
    assert d.status_code == 400
    assert "voip_id" in d.reason.lower()


def test_decide_test_ping_returns_204_no_voip_check(control_session, fernet_env):
    sess, tid = control_session
    secret = get_or_create_webhook_secret(sess, tid)
    # payload=None signals test ping (router short-circuits before calling decide
    # with payload=None for the {"test": 1} case).
    d = decide(tid, secret, None, sess, expected_voip_id=1000000)
    assert d.accepted is True
    assert d.status_code == 204
    assert "test" in d.reason.lower() or "ping" in d.reason.lower()


def test_decide_ok_returns_204(control_session, fernet_env):
    sess, tid = control_session
    secret = get_or_create_webhook_secret(sess, tid)
    d = decide(tid, secret, {"voip_id": 1000000, "event": "call.completed"},
               sess, expected_voip_id=1000000)
    assert d.accepted is True
    assert d.status_code == 204


def test_decide_ok_when_expected_voip_id_is_none(control_session, fernet_env):
    """If we don't know the expected voip_id (rare bootstrap case), accept on path-only."""
    sess, tid = control_session
    secret = get_or_create_webhook_secret(sess, tid)
    d = decide(tid, secret, {"voip_id": "anything"}, sess, expected_voip_id=None)
    assert d.accepted is True
    assert d.status_code == 204


# ---------- module-level discipline ----------

def test_no_hmac_imports():
    """Phone.com doesn't sign webhooks. The module must not import hashlib/hmac
    pretending it does — that would mislead anyone reading the code into thinking
    signing is implemented when it isn't."""
    import gdx_dispatch.modules.phone_com.webhook_signing as ws
    with open(ws.__file__) as _f:
        src = _f.read()
    # Allow comments mentioning hmac for the "we don't use it" explanation,
    # but no actual imports.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        assert "import hmac" not in stripped, f"hmac import forbidden: {line}"
        assert "import hashlib" not in stripped, f"hashlib import forbidden: {line}"
