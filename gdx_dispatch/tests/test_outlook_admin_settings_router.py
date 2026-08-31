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


# ── vendor_bill_sender_allowlist ───────────────────────────────────────
#
# This column gates the whole vendor-bill/statement intake feature, and until
# 2026-07-28 no endpoint read or wrote it — turning the feature on required
# hand-written SQL against prod. These cover the API that replaced that, on a
# REAL session (not a MagicMock) because the round-trip and the row-creation
# commit are exactly what mocks can't prove.

from gdx_dispatch.modules.outlook.admin_settings_router import (  # noqa: E402
    MAX_ALLOWLIST_ENTRIES,
    _clean_allowlist,
    _ensure_settings_row,
)
from gdx_dispatch.modules.outlook.models import OutlookSettings  # noqa: E402


def _real_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase

    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(eng)
    return sessionmaker(bind=eng)


@pytest.fixture
def real_app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    maker = _real_db()
    db = maker()
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_admin_principal] = _admin
    app.dependency_overrides[get_db_for_admin] = lambda: db
    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db, maker


# --- normalization + validation ---------------------------------------------
def test_clean_allowlist_normalizes_and_dedupes():
    assert _clean_allowlist(
        ["  Vendor.COM ", "vendor.com", "AR@Supplier.Net", ""]
    ) == ["vendor.com", "ar@supplier.net"]


@pytest.mark.parametrize("bad", [
    "vendor",             # no dot — a typo, would match nothing
    "com",                # bare TLD
    "two words.com",      # whitespace
    "@vendor.com",        # empty local part
    "vendor.com/path",
    ".vendor.com",
    "vendor..com",
])
def test_clean_allowlist_rejects_entries_that_would_misfire(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _clean_allowlist([bad])
    assert exc.value.status_code == 422


def test_clean_allowlist_caps_the_list():
    from fastapi import HTTPException

    ok = [f"v{i}.com" for i in range(MAX_ALLOWLIST_ENTRIES)]
    assert len(_clean_allowlist(ok)) == MAX_ALLOWLIST_ENTRIES
    with pytest.raises(HTTPException):
        _clean_allowlist(ok + ["one-too-many.com"])


def test_clean_allowlist_accepts_subdomains_and_empty():
    assert _clean_allowlist(["mail.vendor.co.uk"]) == ["mail.vendor.co.uk"]
    assert _clean_allowlist([]) == []          # empty = feature off, not an error


# --- the API round-trip ------------------------------------------------------
def test_patch_then_get_round_trips_the_allowlist(real_app):
    client, db, _ = real_app

    r = client.patch("/api/admin/outlook/settings",
                     json={"vendor_bill_sender_allowlist": ["  Installed.NET  ", "ar@vendor.com"]})
    assert r.status_code == 200
    assert r.json()["vendor_bill_sender_allowlist"] == ["installed.net", "ar@vendor.com"]

    # GET must report it too — it returned nothing at all before this change.
    assert client.get("/api/admin/outlook/settings").json()[
        "vendor_bill_sender_allowlist"
    ] == ["installed.net", "ar@vendor.com"]

    # And it actually landed in the column the background tasks read.
    assert db.get(OutlookSettings, 1).vendor_bill_sender_allowlist == [
        "installed.net", "ar@vendor.com",
    ]


def test_patch_rejects_a_bad_entry_without_changing_what_is_stored(real_app):
    client, db, _ = real_app
    client.patch("/api/admin/outlook/settings",
                 json={"vendor_bill_sender_allowlist": ["good.com"]})

    r = client.patch("/api/admin/outlook/settings",
                     json={"vendor_bill_sender_allowlist": ["good.com", "typo"]})
    assert r.status_code == 422
    db.expire_all()
    assert db.get(OutlookSettings, 1).vendor_bill_sender_allowlist == ["good.com"]


def test_a_rejected_patch_does_not_apply_its_other_fields(real_app):
    """Validation runs before any assignment. Otherwise a 422'd request leaves
    its other mutations sitting on a live ORM object, and nothing but the
    session teardown stops them reaching the DB."""
    client, db, maker = real_app
    client.patch("/api/admin/outlook/settings", json={"backfill_days": 90})

    r = client.patch("/api/admin/outlook/settings",
                     json={"backfill_days": 365, "vendor_bill_sender_allowlist": ["typo"]})
    assert r.status_code == 422

    other = maker()
    try:
        assert other.get(OutlookSettings, 1).backfill_days == 90
    finally:
        other.close()


def test_patch_can_empty_the_allowlist_to_turn_intake_off(real_app):
    client, db, _ = real_app
    client.patch("/api/admin/outlook/settings",
                 json={"vendor_bill_sender_allowlist": ["vendor.com"]})
    r = client.patch("/api/admin/outlook/settings",
                     json={"vendor_bill_sender_allowlist": []})
    assert r.status_code == 200
    assert r.json()["vendor_bill_sender_allowlist"] == []


def test_patching_other_fields_leaves_the_allowlist_alone(real_app):
    """A save from the Tagging tab must not wipe vendor-bill intake."""
    client, db, _ = real_app
    client.patch("/api/admin/outlook/settings",
                 json={"vendor_bill_sender_allowlist": ["vendor.com"]})

    r = client.patch("/api/admin/outlook/settings", json={"backfill_days": 120})
    assert r.status_code == 200
    assert r.json()["vendor_bill_sender_allowlist"] == ["vendor.com"]


# --- the row-creation commit fix --------------------------------------------
def test_reading_settings_actually_persists_the_row(real_app):
    """_ensure_settings_row used to only flush(), so a GET created a row that
    was discarded on session close — the table stayed empty no matter how many
    times the page was opened, and every background task that reads
    OutlookSettings directly saw 'unconfigured'."""
    client, db, maker = real_app
    assert db.query(OutlookSettings).count() == 0

    assert client.get("/api/admin/outlook/settings").status_code == 200

    # A SEPARATE session must see it — that's what "committed" means.
    other = maker()
    try:
        assert other.query(OutlookSettings).count() == 1
    finally:
        other.close()


def test_ensure_settings_row_is_idempotent(real_app):
    _, db, _ = real_app
    first = _ensure_settings_row(db)
    second = _ensure_settings_row(db)
    assert first.id == second.id == 1
    assert db.query(OutlookSettings).count() == 1


# ── Auto-Email tab retired 2026-08-31: one-release transition shape ───────────


def test_patch_accepts_and_ignores_retired_auto_email_triggers(app):
    """A settings tab left open across the deploy still resends this key.
    With extra="forbid" alone, EVERY Outlook tab's Save would 422 until
    reload (one saveSettings serves all tabs). Accepted, never written."""
    client, _, _ = app
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
            json={
                "backfill_days": 45,
                "auto_email_triggers": {"invoice.created": {"subject": "x", "template": "y"}},
            },
        )
    assert r.status_code == 200, r.text
    assert row.backfill_days == 45, "the real field still saves"
    assert row.auto_email_triggers == {}, "the retired key is never written"


def test_get_serves_inert_auto_email_placeholder_for_old_bundles(app):
    """An old bundle re-mounting the retired panel v-models
    settings.auto_email_triggers[trigger].subject — a missing key throws and
    the whole view fails to render. Serve the 3-key shape, inert."""
    client, _, tdb = app
    tdb.query.return_value.filter.return_value.first.return_value = None
    with patch("gdx_dispatch.modules.outlook.admin_settings_router._ensure_settings_row") as ensure:
        row = MagicMock()
        row.backfill_days = None
        row.tag_strategy_order = None
        row.tag_strategy_enabled = None
        row.ai_tag_threshold = None
        row.visibility_rules = None
        row.auto_email_triggers = {"invoice.created": {"subject": "stored-but-unread"}}
        ensure.return_value = row
        r = client.get("/api/admin/outlook/settings")
    assert r.status_code == 200
    body = r.json()["auto_email_triggers"]
    assert set(body) == {"invoice.created", "job.completed", "estimate.sent"}
    assert body["invoice.created"]["subject"] == "", "placeholder, not the stored value — nothing reads it"
