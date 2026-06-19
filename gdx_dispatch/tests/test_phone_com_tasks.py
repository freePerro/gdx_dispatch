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
