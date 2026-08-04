"""Sync-health alarm — born from the 2026-07-30 → 08-04 poison-loop outage.

One duplicated delta message froze 29 folders (including Inbox) for FIVE
DAYS with zero operator-visible signal: the fallback poller reported
"healthy" (it only checked webhook subscription state) and
account.last_error stayed NULL (the crash path bypassed it). These tests
pin the alarm that ends that failure mode:

* partial freeze — some folders advance while others sit >24h behind the
  newest (this outage's exact signature);
* full stall — no folder has synced in >26h;
* account error — reconnect-required style failures;
* never-synced skip folders (Junk, Deleted, …) must NOT alarm;
* the NextAction is a self-clearing singleton and respects a live snooze.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.next_action import NextAction
from gdx_dispatch.modules.outlook.models import (
    OutlookAccount,
    OutlookFolder,
    OutlookFolderSyncState,
)
from gdx_dispatch.modules.outlook.tasks import (
    EMAIL_SYNC_ACTION_TYPE,
    EMAIL_SYNC_REFERENCE_ID,
    _compute_sync_health,
    _upsert_sync_health_action,
    sync_health_check,
)

TENANT = "tenant-1"
NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def tdb():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        OutlookAccount.__table__,
        OutlookFolder.__table__,
        OutlookFolderSyncState.__table__,
        NextAction.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    session = Session()
    yield session
    session.close()


def _mk_account(tdb, *, connected=True, last_error=None):
    a = OutlookAccount(
        user_id=str(uuid4()),
        access_token_enc="fernet-blob" if connected else None,
        last_error=last_error,
    )
    tdb.add(a)
    tdb.commit()
    return a


def _mk_folder(tdb, account, name, *, synced_ago_hours=None, well_known=None):
    """Folder + its sync-state row. synced_ago_hours=None → never synced."""
    f = OutlookFolder(
        account_id=account.id,
        graph_folder_id=f"gf-{name}-{uuid4().hex[:8]}",
        display_name=name,
        well_known_name=well_known,
    )
    tdb.add(f)
    st = OutlookFolderSyncState(
        account_id=account.id,
        folder_id=f.graph_folder_id,
        last_sync_at=(
            None if synced_ago_hours is None
            else NOW - timedelta(hours=synced_ago_hours)
        ),
    )
    tdb.add(st)
    tdb.commit()
    return f


# ── _compute_sync_health ───────────────────────────────────────────────


def test_healthy_when_all_folders_fresh(tdb):
    a = _mk_account(tdb)
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=0.5)
    _mk_folder(tdb, a, "Sent Items", synced_ago_hours=1)
    health = _compute_sync_health(tdb, NOW)
    assert health["status"] == "healthy"
    assert health["problems"] == []
    # newest_sync_at feeds the inbox banner's "last synced" readout
    assert health["newest_sync_at"] == (NOW - timedelta(hours=0.5)).isoformat()


def test_partial_freeze_detected_and_names_folders(tdb):
    """The outage signature: Sent advances while Inbox/Archive sit days
    behind — account-level last_sync_at can NOT see this."""
    a = _mk_account(tdb)
    _mk_folder(tdb, a, "Sent Items", synced_ago_hours=0.5)
    _mk_folder(tdb, a, "Archive", synced_ago_hours=5 * 24)
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=5 * 24)
    health = _compute_sync_health(tdb, NOW)
    assert health["status"] == "unhealthy"
    assert len(health["problems"]) == 1
    assert "2 folder(s) frozen" in health["problems"][0]
    assert "Archive" in health["problems"][0]
    assert "Inbox" in health["problems"][0]


def test_full_stall_detected(tdb):
    a = _mk_account(tdb)
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=3 * 24)
    _mk_folder(tdb, a, "Sent Items", synced_ago_hours=3 * 24)
    health = _compute_sync_health(tdb, NOW)
    assert health["status"] == "unhealthy"
    # One stall problem, not a per-folder repeat of the same fact.
    assert len(health["problems"]) == 1
    assert "no folder has completed a sync since" in health["problems"][0]


def test_skip_folders_never_alarm(tdb):
    """Junk/Deleted are never synced by design — their permanently-stale
    state rows must not page anyone."""
    a = _mk_account(tdb)
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=0.5)
    _mk_folder(tdb, a, "Junk Email", synced_ago_hours=60 * 24, well_known="junkemail")
    _mk_folder(tdb, a, "Deleted Items", synced_ago_hours=None, well_known="deleteditems")
    health = _compute_sync_health(tdb, NOW)
    assert health["status"] == "healthy"


def test_account_error_detected_even_with_fresh_folders(tdb):
    a = _mk_account(tdb, last_error="reconnect required: refresh token expired")
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=0.5)
    health = _compute_sync_health(tdb, NOW)
    assert health["status"] == "unhealthy"
    assert "reconnect required" in health["problems"][0]


def test_no_accounts_is_quiet(tdb):
    assert _compute_sync_health(tdb, NOW)["status"] == "no_accounts"


def test_disconnected_account_is_quiet(tdb):
    _mk_account(tdb, connected=False)
    assert _compute_sync_health(tdb, NOW)["status"] == "no_accounts"


# ── _upsert_sync_health_action ─────────────────────────────────────────


def _open_action(tdb):
    return tdb.execute(
        select(NextAction).where(
            NextAction.action_type == EMAIL_SYNC_ACTION_TYPE,
            NextAction.reference_id == EMAIL_SYNC_REFERENCE_ID,
            NextAction.status != "completed",
        )
    ).scalar_one_or_none()


def test_unhealthy_creates_one_action_then_updates_in_place(tdb):
    assert _upsert_sync_health_action(tdb, TENANT, ["Inbox frozen"]) == "created"
    first = _open_action(tdb)
    assert first is not None
    assert first.priority == "high"
    assert "NOT syncing correctly" in first.description
    assert _upsert_sync_health_action(tdb, TENANT, ["worse now"]) == "updated"
    again = _open_action(tdb)
    assert again.id == first.id  # singleton, not a pile of dupes
    assert "worse now" in again.description


def test_healthy_clears_open_action(tdb):
    _upsert_sync_health_action(tdb, TENANT, ["Inbox frozen"])
    assert _upsert_sync_health_action(tdb, TENANT, []) == "cleared"
    assert _open_action(tdb) is None
    assert _upsert_sync_health_action(tdb, TENANT, []) == "clean"


def test_live_snooze_is_respected(tdb):
    _upsert_sync_health_action(tdb, TENANT, ["Inbox frozen"])
    action = _open_action(tdb)
    action.status = "snoozed"
    action.snoozed_until = datetime.now(timezone.utc) + timedelta(days=1)
    tdb.commit()
    assert _upsert_sync_health_action(tdb, TENANT, ["still frozen"]) == "updated"
    action = _open_action(tdb)
    assert action.status == "snoozed"  # numbers refreshed, nap not cancelled
    assert "still frozen" in action.description


# ── the beat task end-to-end ───────────────────────────────────────────


def test_task_alarms_on_partial_freeze(tdb, caplog):
    a = _mk_account(tdb)
    _mk_folder(tdb, a, "Sent Items", synced_ago_hours=0.5)
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=5 * 24)
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.datetime") as dt:
        dt.now.return_value = NOW
        result = sync_health_check.run()
    assert result["status"] == "unhealthy"
    assert result["action"] == "created"
    assert any(
        "outlook_sync_unhealthy" in r.getMessage() and r.levelname == "ERROR"
        for r in caplog.records
    )


def test_task_is_quiet_when_healthy(tdb, caplog):
    a = _mk_account(tdb)
    _mk_folder(tdb, a, "Inbox", synced_ago_hours=0.5)
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.datetime") as dt:
        dt.now.return_value = NOW
        result = sync_health_check.run()
    assert result["status"] == "healthy"
    assert result["action"] == "clean"
    assert not any(
        r.levelname == "ERROR"
        for r in caplog.records
        if r.name.startswith("gdx_dispatch.modules.outlook")
    )
