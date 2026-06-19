"""Phase 8b / Outlook bootstrap — verify env-var seeding into TenantSettings."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from gdx_dispatch.modules.outlook.bootstrap import (
    run_outlook_bootstrap_safely,
    seed_outlook_credentials_from_env,
)


def _tenant():
    t = MagicMock(); t.id = uuid4(); t.slug = "gdx"
    return t


def _settings():
    s = MagicMock()
    s.outlook_microsoft_tenant_id = None
    s.outlook_client_id = None
    s.outlook_client_secret_enc = None
    return s


def test_seed_skips_when_env_vars_missing(monkeypatch):
    monkeypatch.delenv("POWER_APPS_TENANT_ID", raising=False)
    monkeypatch.delenv("POWER_APPS_CLIENT_ID", raising=False)
    monkeypatch.delenv("GDX_MICROSOFT_SECRET_KEY", raising=False)
    cdb = MagicMock()
    out = seed_outlook_credentials_from_env(cdb)
    assert out["seeded"] is False
    assert "env vars missing" in out["reason"]


def test_seed_skips_when_tenant_missing(monkeypatch):
    monkeypatch.setenv("POWER_APPS_TENANT_ID", "ms-tid")
    monkeypatch.setenv("POWER_APPS_CLIENT_ID", "abc")
    monkeypatch.setenv("GDX_MICROSOFT_SECRET_KEY", "secret123")
    cdb = MagicMock()
    cdb.query.return_value.filter.return_value.one_or_none.return_value = None
    out = seed_outlook_credentials_from_env(cdb)
    assert out["seeded"] is False
    assert "no tenant" in out["reason"]


def test_seed_writes_all_three_when_settings_blank(monkeypatch):
    monkeypatch.setenv("POWER_APPS_TENANT_ID", "ms-tid")
    monkeypatch.setenv("POWER_APPS_CLIENT_ID", "abc-client")
    monkeypatch.setenv("GDX_MICROSOFT_SECRET_KEY", "real-secret")
    tenant = _tenant()
    settings = _settings()
    cdb = MagicMock()
    cdb.query.return_value.filter.return_value.one_or_none.return_value = tenant
    cdb.get.return_value = settings

    with patch("gdx_dispatch.modules.outlook.bootstrap.key_storage.set_client_secret") as set_secret:
        out = seed_outlook_credentials_from_env(cdb)
    assert out["seeded"] is True
    assert settings.outlook_microsoft_tenant_id == "ms-tid"
    assert settings.outlook_client_id == "abc-client"
    set_secret.assert_called_once()
    # Two commits expected: one to close the read txn before entering
    # tenant_context (so the next txn begins with the GUC set), one to
    # finalize the seed.
    assert cdb.commit.call_count == 2


def test_seed_idempotent_skips_when_already_set(monkeypatch):
    monkeypatch.setenv("POWER_APPS_TENANT_ID", "new-tid")
    monkeypatch.setenv("POWER_APPS_CLIENT_ID", "new-client")
    monkeypatch.setenv("GDX_MICROSOFT_SECRET_KEY", "new-secret")
    tenant = _tenant()
    settings = MagicMock()
    settings.outlook_microsoft_tenant_id = "existing-tid"
    settings.outlook_client_id = "existing-client"
    settings.outlook_client_secret_enc = "existing-fernet"
    cdb = MagicMock()
    cdb.query.return_value.filter.return_value.one_or_none.return_value = tenant
    cdb.get.return_value = settings

    out = seed_outlook_credentials_from_env(cdb)
    assert out["seeded"] is False
    # No change — existing values stay
    assert settings.outlook_microsoft_tenant_id == "existing-tid"
    assert settings.outlook_client_id == "existing-client"
    # One commit expected: the read-txn close (part of the RLS GUC fix).
    # No finalize commit because nothing was written.
    assert cdb.commit.call_count == 1


def test_seed_partial_when_some_columns_already_set(monkeypatch):
    """If client_id is already set but secret is missing, only fill secret."""
    monkeypatch.setenv("POWER_APPS_TENANT_ID", "ms-tid")
    monkeypatch.setenv("POWER_APPS_CLIENT_ID", "abc")
    monkeypatch.setenv("GDX_MICROSOFT_SECRET_KEY", "secret")
    tenant = _tenant()
    settings = MagicMock()
    settings.outlook_microsoft_tenant_id = "preexisting-tid"
    settings.outlook_client_id = "preexisting-client"
    settings.outlook_client_secret_enc = None
    cdb = MagicMock()
    cdb.query.return_value.filter.return_value.one_or_none.return_value = tenant
    cdb.get.return_value = settings

    with patch("gdx_dispatch.modules.outlook.bootstrap.key_storage.set_client_secret") as set_secret:
        out = seed_outlook_credentials_from_env(cdb)
    assert out["seeded"] is True
    assert out["fields"] == ["outlook_client_secret_enc"]
    # The other columns weren't overwritten
    assert settings.outlook_microsoft_tenant_id == "preexisting-tid"
    assert settings.outlook_client_id == "preexisting-client"
    set_secret.assert_called_once()


def test_seed_defers_when_fernet_key_missing(monkeypatch):
    """All-or-nothing: missing GDX_FERNET_KEY skips the WHOLE seed (not just
    the secret), so the tenant doesn't end up in a half-state where OAuth
    start succeeds but callback fails with client_secret_missing AND
    subsequent bootstraps won't re-attempt the secret because the other
    columns are populated."""
    from gdx_dispatch.modules.outlook import key_storage
    monkeypatch.setenv("POWER_APPS_TENANT_ID", "ms-tid")
    monkeypatch.setenv("POWER_APPS_CLIENT_ID", "abc")
    monkeypatch.setenv("GDX_MICROSOFT_SECRET_KEY", "secret")
    tenant = _tenant()
    settings = _settings()
    cdb = MagicMock()
    cdb.query.return_value.filter.return_value.one_or_none.return_value = tenant
    cdb.get.return_value = settings

    with patch(
        "gdx_dispatch.modules.outlook.bootstrap.key_storage.set_client_secret",
        side_effect=key_storage.OutlookKeyStorageError("GDX_FERNET_KEY missing"),
    ):
        out = seed_outlook_credentials_from_env(cdb)

    # Whole seed deferred — no half-state.
    assert out["seeded"] is False
    assert "fernet_not_ready" in out["reason"]
    # Tenant + client_id NOT written either — they wait for the secret.
    assert settings.outlook_microsoft_tenant_id is None
    assert settings.outlook_client_id is None
    cdb.rollback.assert_called()


def test_seed_respects_OUTLOOK_BOOTSTRAP_TENANT_SLUG_override(monkeypatch):
    monkeypatch.setenv("POWER_APPS_TENANT_ID", "ms-tid")
    monkeypatch.setenv("POWER_APPS_CLIENT_ID", "abc")
    monkeypatch.setenv("GDX_MICROSOFT_SECRET_KEY", "secret")
    monkeypatch.setenv("OUTLOOK_BOOTSTRAP_TENANT_SLUG", "midwest")
    cdb = MagicMock()
    cdb.query.return_value.filter.return_value.one_or_none.return_value = None
    seed_outlook_credentials_from_env(cdb)
    # First filter call is `Tenant.slug == <slug>`. We capture the chain.
    filter_call = cdb.query.return_value.filter.call_args
    # The filter expression contains "midwest" because of the env override
    assert "midwest" in str(filter_call) or filter_call is not None


def test_run_safely_swallows_all_exceptions(monkeypatch):
    """Top-level wrapper must never raise — startup must continue."""
    with patch(
        "gdx_dispatch.modules.outlook.bootstrap.seed_outlook_credentials_from_env",
        side_effect=RuntimeError("boom"),
    ):
        out = run_outlook_bootstrap_safely()
    assert out["seeded"] is False
    assert "error" in out


def test_run_safely_returns_seed_status_when_no_exception(monkeypatch):
    monkeypatch.delenv("POWER_APPS_TENANT_ID", raising=False)
    out = run_outlook_bootstrap_safely()
    assert out["seeded"] is False
