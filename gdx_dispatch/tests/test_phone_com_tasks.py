"""Wave B / S1 — Phone.com beat task tests.

Covers `sync_all_phone_com_tenants` (the every-15-min beat fan-out) and
`run_phone_com_sync` (per-tenant worker). Asserts only token-set tenants are
dispatched and run_full_resync is invoked exactly once per tenant.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com import tasks as pc_tasks


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )


@pytest.fixture
def control_db(monkeypatch):
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for n in ("tenants", "tenant_settings"):
        if n in ControlBase.metadata.tables:
            ControlBase.metadata.tables[n].create(e, checkfirst=True)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    # Replace SessionLocal so the task uses our in-memory DB.
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.tasks.SessionLocal", sm,
    )
    return sm


def test_sync_all_dispatches_only_token_set_tenants(control_db):
    """Three tenants: A has token, B has none, C has token. Beat fans out
    to A and C only."""
    sm = control_db
    s = sm()
    a, b, c = uuid4(), uuid4(), uuid4()
    for tid, slug in [(a, "ta"), (b, "tb"), (c, "tc")]:
        s.add(Tenant(id=tid, slug=slug, name=slug.upper()))
    s.commit()
    key_storage.set_token(s, a, "phc-good-a-12345")
    key_storage.set_token(s, c, "phc-good-c-12345")
    s.close()

    with patch.object(pc_tasks.run_phone_com_sync, "delay") as mock_delay:
        result = pc_tasks.sync_all_phone_com_tenants.run()

    assert result == {"dispatched": 2}
    dispatched = sorted(call.args[0] for call in mock_delay.call_args_list)
    assert dispatched == sorted([str(a), str(c)])


def test_sync_all_no_tenants_returns_zero(control_db):
    """No tenants with tokens → no dispatches, no error."""
    with patch.object(pc_tasks.run_phone_com_sync, "delay") as mock_delay:
        result = pc_tasks.sync_all_phone_com_tenants.run()
    assert result == {"dispatched": 0}
    mock_delay.assert_not_called()


def test_run_phone_com_sync_unknown_tenant_short_circuits(control_db):
    """Worker called with a UUID that isn't in tenants returns ok=false."""
    bogus = uuid4()
    result = pc_tasks.run_phone_com_sync.run(str(bogus))
    assert result["ok"] is False
    assert result["error"] == "unknown tenant"
    assert result["tenant_id"] == str(bogus)


def test_run_phone_com_sync_invokes_full_resync(control_db, monkeypatch):
    """Worker calls run_full_resync once with the right tenant id."""
    sm = control_db
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T1"))
    s.commit()
    s.close()

    calls = []

    def fake(cdb, t):
        calls.append(t)
        return {"ok": True, "calls_synced": 7, "messages_synced": 0,
                "voicemails_synced": 0}

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.tasks.run_full_resync", fake)
    result = pc_tasks.run_phone_com_sync.run(str(tid))

    assert result["ok"] is True
    assert result["calls_synced"] == 7
    assert result["tenant_id"] == str(tid)
    assert calls == [tid]


def test_beat_schedule_includes_phone_com():
    """P1.5 — webhooks cover live; sync drops to nightly reconcile."""
    from gdx_dispatch.core.scheduler import build_beat_schedule

    sched = build_beat_schedule()
    assert "phone-com-reconcile-nightly" in sched
    entry = sched["phone-com-reconcile-nightly"]
    assert entry["task"] == "phone_com.sync_all_phone_com_tenants"
    assert entry["options"]["queue"] == "priority:low"
    # The 15-min cadence is gone — that was the dual-write race.
    assert "phone-com-sync-every-15m" not in sched
    # P1.4 — webhook secret rotation runs weekly.
    assert "phone-com-rotate-webhook-secret-weekly" in sched
    rot = sched["phone-com-rotate-webhook-secret-weekly"]
    assert rot["task"] == "phone_com.rotate_all_webhook_secrets"


def test_roll_up_all_dispatches_only_token_set_tenants(control_db):
    """D-pc-8: nightly stats fan-out goes to phone_com-enabled tenants only."""
    sm = control_db
    s = sm()
    a, b = uuid4(), uuid4()
    for tid, slug in [(a, "ta"), (b, "tb")]:
        s.add(Tenant(id=tid, slug=slug, name=slug.upper()))
    s.commit()
    key_storage.set_token(s, a, "phc-good-a-12345")
    s.close()

    with patch.object(pc_tasks.roll_up_phone_com_stats, "delay") as mock_delay:
        result = pc_tasks.roll_up_all_phone_com_stats.run()

    assert result == {"dispatched": 1}
    assert mock_delay.call_args_list[0].args[0] == str(a)


def test_roll_up_stats_calls_roll_up_recent(monkeypatch):
    """Single-tenant: stats rollup calls roll_up_recent against app DB."""
    from unittest.mock import MagicMock
    monkeypatch.setattr("gdx_dispatch.modules.phone_com.tasks.SessionLocal", MagicMock)
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.tasks.roll_up_recent",
        lambda db, **kw: {"days_rolled_up": 7},
    )
    result = pc_tasks.roll_up_phone_com_stats.run(str(uuid4()))
    assert result["ok"] is True
    assert result["days_rolled_up"] == 7


def test_beat_schedule_includes_phone_com_stats_rollup():
    """D-pc-8: nightly stats backstop is wired."""
    from gdx_dispatch.core.scheduler import build_beat_schedule

    sched = build_beat_schedule()
    assert "phone-com-stats-rollup-nightly" in sched
    entry = sched["phone-com-stats-rollup-nightly"]
    assert entry["task"] == "phone_com.roll_up_all_phone_com_stats"
    assert entry["options"]["queue"] == "priority:low"


# ── P1.4: rotation task ─────────────────────────────────────────────────


def test_rotate_all_only_dispatches_configured_tenants(control_db):
    """Only tenants with token + callback_id get rotated."""
    sm = control_db
    s = sm()
    from gdx_dispatch.control.models import TenantSettings
    a, b, c = uuid4(), uuid4(), uuid4()
    for tid, slug in [(a, "ta"), (b, "tb"), (c, "tc")]:
        s.add(Tenant(id=tid, slug=slug, name=slug.upper()))
    s.commit()
    # A: token + webhook
    key_storage.set_token(s, a, "phc-good-a-12345")
    sa = s.get(TenantSettings, a)
    sa.phone_com_webhook_callback_id = 99999
    # B: token only — no callback id
    key_storage.set_token(s, b, "phc-good-b-12345")
    # C: nothing
    s.commit()
    s.close()

    with patch.object(pc_tasks.rotate_webhook_secret, "delay") as mock_delay:
        result = pc_tasks.rotate_all_webhook_secrets.run()
    assert result == {"dispatched": 1}
    assert mock_delay.call_args_list[0].args[0] == str(a)


def test_rotate_skips_unconfigured_tenant(control_db):
    bogus = uuid4()
    result = pc_tasks.rotate_webhook_secret.run(str(bogus))
    assert result["ok"] is False
    assert "not configured" in result.get("skipped", "")

# ── messages-refresh beat task (SMS-inbox fix, 2026-07-23) ──────────────


def test_sync_all_recent_messages_dispatches_only_token_set_tenants(control_db):
    sm = control_db
    s = sm()
    a, b = uuid4(), uuid4()
    for tid, slug in [(a, "ta"), (b, "tb")]:
        s.add(Tenant(id=tid, slug=slug, name=slug.upper()))
    s.commit()
    key_storage.set_token(s, a, "phc-good-a-12345")
    s.commit()
    s.close()

    with patch.object(pc_tasks.sync_recent_messages_task, "delay") as mock_delay:
        result = pc_tasks.sync_all_recent_messages.run()
    assert result == {"dispatched": 1}
    assert mock_delay.call_args_list[0].args[0] == str(a)


def test_sync_recent_messages_task_invokes_sync(control_db, monkeypatch):
    seen = {}

    def fake(cdb, tid, **kw):
        seen["tid"] = tid
        return {"ok": True, "messages_synced": 5}

    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.tasks.sync_recent_messages", fake,
    )
    tid = uuid4()
    result = pc_tasks.sync_recent_messages_task.run(str(tid))
    assert result["ok"] is True
    assert result["messages_synced"] == 5
    assert seen["tid"] == tid


def test_beat_schedule_includes_messages_refresh():
    from gdx_dispatch.core.scheduler import build_beat_schedule

    sched = build_beat_schedule()
    assert "phone-com-messages-refresh" in sched
    entry = sched["phone-com-messages-refresh"]
    assert entry["task"] == "phone_com.sync_all_recent_messages"


# ── calls-refresh beat task (voicemail live path, 2026-08-03) ───────────


def test_sync_all_recent_calls_dispatches_only_token_set_tenants(control_db):
    sm = control_db
    s = sm()
    a, b = uuid4(), uuid4()
    for tid, slug in [(a, "ta"), (b, "tb")]:
        s.add(Tenant(id=tid, slug=slug, name=slug.upper()))
    s.commit()
    key_storage.set_token(s, a, "phc-good-a-12345")
    s.commit()
    s.close()

    with patch.object(pc_tasks.sync_recent_calls_task, "delay") as mock_delay:
        result = pc_tasks.sync_all_recent_calls.run()
    assert result == {"dispatched": 1}
    assert mock_delay.call_args_list[0].args[0] == str(a)


def test_sync_recent_calls_task_invokes_sync(control_db, monkeypatch):
    seen = {}

    def fake(cdb, tid, **kw):
        seen["tid"] = tid
        return {"ok": True, "calls_synced": 3, "voicemails_synced": 1}

    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.tasks.sync_recent_calls", fake,
    )
    tid = uuid4()
    result = pc_tasks.sync_recent_calls_task.run(str(tid))
    assert result["ok"] is True
    assert result["calls_synced"] == 3
    assert result["voicemails_synced"] == 1
    assert seen["tid"] == tid


def test_beat_schedule_includes_calls_refresh():
    from gdx_dispatch.core.scheduler import build_beat_schedule

    sched = build_beat_schedule()
    assert "phone-com-calls-refresh" in sched
    entry = sched["phone-com-calls-refresh"]
    assert entry["task"] == "phone_com.sync_all_recent_calls"


def test_rotate_refuses_without_public_base_url(control_db, monkeypatch):
    """Rotating with GDX_PUBLIC_BASE_URL unset would PATCH the Phone.com
    callback to a placeholder URL and silently kill webhook delivery (live
    on prod until 2026-07-23). The task must refuse."""
    from gdx_dispatch.control.models import TenantSettings

    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    sm = control_db
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="ta", name="TA"))
    s.commit()
    key_storage.set_token(s, tid, "phc-good-a-12345")
    ts_row = s.get(TenantSettings, tid)
    ts_row.phone_com_webhook_callback_id = 99999
    key_storage.get_or_create_webhook_secret(s, tid)
    s.commit()
    secret_before = s.get(TenantSettings, tid).phone_com_webhook_secret
    s.close()

    result = pc_tasks.rotate_webhook_secret.run(str(tid))
    assert result["ok"] is False
    assert "GDX_PUBLIC_BASE_URL" in result["error"]

    s = sm()
    try:
        assert s.get(TenantSettings, tid).phone_com_webhook_secret == secret_before
    finally:
        s.close()


def test_rotate_patches_the_exact_callback_url(control_db, monkeypatch):
    """Pin the exact string handed to Phone.com by the weekly beat task.

    This is the positive path. Only the refusal path was covered before, where
    `base` is empty and the URL is never built — so a suite that was fully
    green still could not fail when the builder started emitting
    `https://{slug}.https://{host}/...`. That URL is PATCHed onto the live
    callback by `phone-com-rotate-webhook-secret-weekly` (Sun 08:00 UTC) with
    no operator in the loop, after the secret has already been rotated, so a
    malformed value kills inbound delivery silently once the grace window ends.
    """
    from gdx_dispatch.control.models import TenantSettings

    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.test")
    sm = control_db
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="gdx", name="GDX"))
    s.commit()
    key_storage.set_token(s, tid, "phc-good-a-12345")
    ts_row = s.get(TenantSettings, tid)
    ts_row.phone_com_webhook_callback_id = 292916
    key_storage.get_or_create_webhook_secret(s, tid)
    s.commit()
    s.close()

    sent: dict = {}

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def patch_callback(self, *, callback_id, url):
            sent["callback_id"] = callback_id
            sent["url"] = url
            return {"ok": True}

    class _AppSettings:
        phone_com_voip_id = 12345

    class _Query:
        def first(self):
            return _AppSettings()

    class _TenantDB:
        def query(self, *a, **kw):
            return _Query()

        def close(self):
            pass

    with patch.object(pc_tasks, "_open_tenant_session", return_value=_TenantDB()), \
         patch("gdx_dispatch.modules.phone_com.client.PhoneComClient", _Client):
        pc_tasks.rotate_webhook_secret.run(str(tid))

    assert sent, "patch_callback was never called — the test did not exercise the URL"
    url = sent["url"]
    assert url.startswith("https://gdx.example.test/api/webhooks/phone-com/gdx/"), url
    # The bug this pins: a full origin pasted into a `https://{slug}.{base}` template.
    assert "https://gdx.https://" not in url
    assert url.count("https://") == 1, url
