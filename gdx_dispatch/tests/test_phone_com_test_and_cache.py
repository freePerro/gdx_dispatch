"""pc-s2b — key_storage.test_and_cache_account: validate token + cache features + mark."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant, TenantSettings
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com.client import BASE_URL
from gdx_dispatch.modules.phone_com.key_storage import set_token
from gdx_dispatch.modules.phone_com.key_storage import (
    test_and_cache_account as run_test_and_cache,
)

_ACCT = {
    "filters": {}, "sort": {"id": "desc"}, "total": 1, "limit": 25, "offset": None,
    "items": [{"id": 1000000, "name": "Example Owner",
               "features": {"call-recording-on": False}}],
}


@pytest.fixture
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    """Bypass audit_logs DDL — sqlite test DB doesn't have the prod-only guard schema."""
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None,
        raising=False,
    )


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
def tenant_engine_factory(monkeypatch):
    """Stub SessionLocal so the impl opens a session against an in-memory SQLite DB."""
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.key_storage._SessionLocal", sm)
    return sm


def test_no_token_returns_ok_false_no_http(control_session, fernet_env, tenant_engine_factory):
    sess, tid = control_session
    with respx.mock() as router:
        result = run_test_and_cache(sess, tid)
    assert result["ok"] is False
    assert "no token" in result["error"].lower()
    assert result["features_cached"] is False
    assert len(router.calls) == 0  # no upstream call when no token


@respx.mock
def test_success_caches_features_marks_validated(control_session, fernet_env, tenant_engine_factory):
    sess, tid = control_session
    set_token(sess, tid, "phc-good")
    respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(200, json=_ACCT))

    result = run_test_and_cache(sess, tid)
    assert result["ok"] is True
    assert result["voip_id"] == 1000000
    assert result["features_cached"] is True

    # Control-plane marked validated
    settings = sess.get(TenantSettings, tid)
    assert settings.phone_com_token_last_validated_at is not None
    assert settings.phone_com_token_last_error is None

    # Tenant-plane AppSettings got the features
    tenant_sess = tenant_engine_factory()
    app = tenant_sess.query(AppSettings).first()
    assert app is not None
    assert app.phone_com_account_features == {"call-recording-on": False}
    assert app.phone_com_voip_id == "1000000" or app.phone_com_voip_id == 1000000
    tenant_sess.close()


@respx.mock
def test_failure_marks_failed_no_features_written(control_session, fernet_env, tenant_engine_factory):
    sess, tid = control_session
    set_token(sess, tid, "phc-bad")
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"}))

    result = run_test_and_cache(sess, tid)
    assert result["ok"] is False
    assert result["features_cached"] is False

    settings = sess.get(TenantSettings, tid)
    assert settings.phone_com_token_last_error is not None
    assert settings.phone_com_token_last_validated_at is None

    # No AppSettings row written on failure
    tenant_sess = tenant_engine_factory()
    rows = tenant_sess.query(AppSettings).all()
    # If a row exists, features should NOT be set on it
    if rows:
        assert rows[0].phone_com_account_features is None
    tenant_sess.close()


@respx.mock
def test_does_not_overwrite_existing_voip_id(control_session, fernet_env, tenant_engine_factory):
    """If voip_id is already set on AppSettings, don't clobber it."""
    sess, tid = control_session
    set_token(sess, tid, "phc-good")
    sm = tenant_engine_factory
    s = sm()
    s.add(AppSettings(phone_com_voip_id="999999"))  # pre-existing  # noqa: pre-existing voip
    s.commit()
    s.close()

    respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(200, json=_ACCT))

    run_test_and_cache(sess, tid)

    s = sm()
    app = s.query(AppSettings).first()
    assert str(app.phone_com_voip_id) == "999999"  # unchanged
    s.close()
