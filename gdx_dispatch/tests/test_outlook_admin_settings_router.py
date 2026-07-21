"""Phase 8 backend / Outlook admin settings + credentials endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.modules.outlook.admin_settings_router import (
    get_admin_principal,
    get_db_for_admin,
    get_db_for_admin,
    router as admin_router,
)
from gdx_dispatch.routers.auth import get_current_user


TID = uuid4()


def _admin():
    return {"user_id": str(uuid4()), "tenant_id": str(TID), "role": "admin"}


def _tech():
    return {"user_id": str(uuid4()), "tenant_id": str(TID), "role": "technician"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()
    app.include_router(admin_router)
    # Single-tenant collapse: control + tenant planes are one DB, and the
    # router exposes a single db dependency (get_db_for_admin). Use one
    # MagicMock for both planes so configuring either `cdb` or `tdb` in a
    # test reaches the dependency the router actually resolves.
    db = MagicMock()
    cdb = tdb = db
    app.dependency_overrides[get_admin_principal] = _admin
    app.dependency_overrides[get_db_for_admin] = lambda: db
    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), cdb, tdb


# ── settings GET/PATCH ─────────────────────────────────────────────────


def test_get_settings_returns_defaults_when_row_missing(app):
    client, _, tdb = app
    tdb.query.return_value.filter.return_value.first.return_value = None
    with patch("gdx_dispatch.modules.outlook.admin_settings_router._ensure_settings_row") as ensure:
        row = MagicMock()
        row.backfill_days = None
        row.tag_strategy_order = None
        row.tag_strategy_enabled = None
        row.ai_tag_threshold = None
        row.visibility_rules = None
        row.auto_email_triggers = None
        ensure.return_value = row
        r = client.get("/api/admin/outlook/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["backfill_days"] == 90
    assert body["tag_strategy_order"] == ["auto_match", "job_thread", "ai"]
    assert body["ai_tag_threshold"] == 0.85


def test_patch_settings_updates_provided_fields(app):
    client, _, tdb = app
    row = MagicMock()
    row.backfill_days = 90
    row.tag_strategy_order = ["auto_match", "job_thread", "ai"]
    row.tag_strategy_enabled = {"auto_match": True, "job_thread": True, "ai": True}
    row.ai_tag_threshold = None
    row.visibility_rules = {}
    row.auto_email_triggers = {}
    with patch("gdx_dispatch.modules.outlook.admin_settings_router._ensure_settings_row", return_value=row):
        r = client.patch(
            "/api/admin/outlook/settings",
            json={"backfill_days": 30, "ai_tag_threshold": 0.9},
        )
    assert r.status_code == 200
    assert row.backfill_days == 30


def test_patch_settings_rejects_extra_fields(app):
    client, _, _ = app
    r = client.patch("/api/admin/outlook/settings", json={"unknown_field": True})
    assert r.status_code == 422


# ── credentials GET/PATCH/DELETE ───────────────────────────────────────


def test_get_credentials_returns_secret_set_false_when_unset(app):
    client, cdb, _ = app
    cdb.get.return_value = None
    r = client.get("/api/admin/outlook/credentials")
    assert r.status_code == 200
    body = r.json()
    assert body["secret_set"] is False
    assert body["microsoft_tenant_id"] is None
    assert body["client_id"] is None


def test_get_credentials_never_returns_actual_secret(app):
    client, cdb, _ = app
    settings = MagicMock()
    settings.outlook_microsoft_tenant_id = "ms-tid"
    settings.outlook_client_id = "abc"
    settings.outlook_client_secret_enc = "fernet-very-secret-ciphertext"
    settings.outlook_secret_set_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    cdb.get.return_value = settings
    r = client.get("/api/admin/outlook/credentials")
    body = r.json()
    assert body["secret_set"] is True
    assert "fernet" not in r.text
    assert "secret_enc" not in r.text


def test_patch_credentials_sets_client_id_and_secret(app):
    client, cdb, _ = app
    settings = MagicMock()
    settings.outlook_microsoft_tenant_id = None
    settings.outlook_client_id = None
    settings.outlook_client_secret_enc = None
    settings.outlook_secret_set_at = None
    cdb.get.return_value = settings
    with patch("gdx_dispatch.modules.outlook.admin_settings_router.key_storage.set_client_secret") as set_secret:
        r = client.patch(
            "/api/admin/outlook/credentials",
            json={"microsoft_tenant_id": "ms-tid", "client_id": "abc", "client_secret": "real-secret-1234567890"},
        )
    assert r.status_code == 200
    assert settings.outlook_microsoft_tenant_id == "ms-tid"
    assert settings.outlook_client_id == "abc"
    set_secret.assert_called_once()


def test_delete_credentials_clears_secret(app):
    client, cdb, _ = app
    with patch("gdx_dispatch.modules.outlook.admin_settings_router.key_storage.clear_client_secret") as clear:
        r = client.delete("/api/admin/outlook/credentials")
    assert r.status_code == 204
    clear.assert_called_once()


# ── auth gate ──────────────────────────────────────────────────────────


def test_settings_blocked_for_non_admin():
    monkey_app = FastAPI()
    monkey_app.include_router(admin_router)
    cdb = MagicMock()
    tdb = MagicMock()
    monkey_app.dependency_overrides[get_admin_principal] = lambda: (
        _ for _ in ()
    ).throw(__import__("fastapi").HTTPException(status_code=403, detail="admin only"))
    monkey_app.dependency_overrides[get_db_for_admin] = lambda: cdb
    monkey_app.dependency_overrides[get_db_for_admin] = lambda: tdb
    client = TestClient(monkey_app)
    r = client.get("/api/admin/outlook/settings")
    assert r.status_code == 403


# ── POST /vendor-bills/sweep (Phase 2, D3) ─────────────────────────────


def test_sweep_queues_task_per_connected_account(app):
    client, _, tdb = app
    settings = MagicMock()
    settings.vendor_bill_sender_allowlist = ["midwest.com"]
    tdb.get.return_value = settings
    acct = MagicMock()
    acct.id = uuid4()
    tdb.query.return_value.filter.return_value.all.return_value = [acct]
    with patch("gdx_dispatch.modules.outlook.tasks.sweep_vendor_bill_history") as task:
        task.delay.return_value = MagicMock(id="celery-task-1")
        r = client.post("/api/admin/outlook/vendor-bills/sweep", json={"days": 180})
    assert r.status_code == 202
    body = r.json()
    assert body["days"] == 180
    assert body["queued"] == [{"account_id": str(acct.id), "task_id": "celery-task-1"}]
    task.delay.assert_called_once_with(str(acct.id), str(TID), days=180)


def test_sweep_defaults_to_365_days_with_no_body(app):
    client, _, tdb = app
    settings = MagicMock()
    settings.vendor_bill_sender_allowlist = ["midwest.com"]
    tdb.get.return_value = settings
    acct = MagicMock()
    acct.id = uuid4()
    tdb.query.return_value.filter.return_value.all.return_value = [acct]
    with patch("gdx_dispatch.modules.outlook.tasks.sweep_vendor_bill_history") as task:
        task.delay.return_value = MagicMock(id="t")
        r = client.post("/api/admin/outlook/vendor-bills/sweep")
    assert r.status_code == 202
    assert r.json()["days"] == 365
    task.delay.assert_called_once_with(str(acct.id), str(TID), days=365)


def test_sweep_400_when_allowlist_empty(app):
    client, _, tdb = app
    settings = MagicMock()
    settings.vendor_bill_sender_allowlist = []
    tdb.get.return_value = settings
    with patch("gdx_dispatch.modules.outlook.tasks.sweep_vendor_bill_history") as task:
        r = client.post("/api/admin/outlook/vendor-bills/sweep", json={})
    assert r.status_code == 400
    assert "allowlist" in r.json()["detail"]
    task.delay.assert_not_called()


def test_sweep_400_when_no_settings_row(app):
    client, _, tdb = app
    tdb.get.return_value = None  # feature never configured
    with patch("gdx_dispatch.modules.outlook.tasks.sweep_vendor_bill_history") as task:
        r = client.post("/api/admin/outlook/vendor-bills/sweep", json={})
    assert r.status_code == 400
    task.delay.assert_not_called()


def test_sweep_404_when_no_connected_account(app):
    client, _, tdb = app
    settings = MagicMock()
    settings.vendor_bill_sender_allowlist = ["midwest.com"]
    tdb.get.return_value = settings
    tdb.query.return_value.filter.return_value.all.return_value = []
    with patch("gdx_dispatch.modules.outlook.tasks.sweep_vendor_bill_history") as task:
        r = client.post("/api/admin/outlook/vendor-bills/sweep", json={})
    assert r.status_code == 404
    task.delay.assert_not_called()
