"""Phase 2 — Celery sync/backfill/renew/poll tasks for Outlook integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from gdx_dispatch.modules.outlook.tasks import (
    _extract_delta_token,
    _parse_iso,
    _persist_messages,
)


# ── helpers ────────────────────────────────────────────────────────────


def test_parse_iso_handles_z_suffix():
    dt = _parse_iso("2026-04-27T10:00:00Z")
    assert dt is not None and dt.year == 2026


def test_parse_iso_returns_none_for_garbage():
    assert _parse_iso(None) is None
    assert _parse_iso("") is None
    assert _parse_iso("not-a-date") is None


def test_extract_delta_token_parses_query():
    assert _extract_delta_token("https://x?$deltatoken=abc123") == "abc123"
    assert _extract_delta_token(None) is None
    assert _extract_delta_token("https://x") is None


# ── _persist_messages ──────────────────────────────────────────────────


def test_persist_messages_inserts_new_row():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock(); account.id = uuid4()
    n = _persist_messages(tdb, account, [
        {"id": "g1", "subject": "Re: estimate",
         "from": {"emailAddress": {"address": "alice@x.com"}}},
    ])
    assert n == 1
    tdb.add.assert_called_once()


def test_persist_messages_updates_existing():
    tdb = MagicMock()
    existing = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = existing
    account = MagicMock(); account.id = uuid4()
    n = _persist_messages(tdb, account, [{"id": "g1", "subject": "updated"}])
    assert n == 1
    tdb.add.assert_not_called()
    assert existing.subject == "updated"


def test_persist_messages_skips_when_no_id():
    tdb = MagicMock()
    account = MagicMock(); account.id = uuid4()
    assert _persist_messages(tdb, account, [{"subject": "no id"}]) == 0


def test_persist_messages_records_body_size_when_body_present():
    """body_size_bytes is recorded but body_r2_key stays NULL until the
    R2 upload path lands. Setting r2_key without an actual upload would
    cause downstream 404s when the blob is fetched."""
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock(); account.id = uuid4(); account.upn = "doug@gdx.com"
    _persist_messages(tdb, account, [
        {"id": "g1", "body": {"content": "<html>hi</html>", "contentType": "html"}},
    ])
    row = tdb.add.call_args.args[0]
    assert row.body_size_bytes == len("<html>hi</html>")
    # body_r2_key intentionally not set — no R2 upload yet
    assert getattr(row, "body_r2_key", None) is None or not isinstance(row.body_r2_key, str)


def test_persist_messages_sets_direction_outbound_when_sender_is_owner():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock(); account.id = uuid4(); account.upn = "doug@gdx.com"
    _persist_messages(tdb, account, [
        {"id": "g1", "from": {"emailAddress": {"address": "doug@gdx.com"}}},
    ])
    row = tdb.add.call_args.args[0]
    assert row.direction == "outbound"


def test_persist_messages_sets_direction_inbound_when_sender_is_external():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock(); account.id = uuid4(); account.upn = "doug@gdx.com"
    _persist_messages(tdb, account, [
        {"id": "g1", "from": {"emailAddress": {"address": "alice@external.com"}}},
    ])
    row = tdb.add.call_args.args[0]
    assert row.direction == "inbound"


# ── sync_outlook_mailbox ───────────────────────────────────────────────


def test_sync_outlook_mailbox_skips_when_no_account():
    from gdx_dispatch.modules.outlook import tasks
    aid, tid = uuid4(), uuid4()
    tdb = MagicMock(); tdb.get.return_value = None
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb):
        result = tasks.sync_outlook_mailbox.run(str(aid), str(tid))
    assert result["messages_upserted"] == 0
    assert result["skipped"] == "no account"


def test_sync_outlook_mailbox_walks_folders_and_returns_aggregate():
    """Multi-folder sync: folder cache refresh + per-folder delta walk +
    aggregate counters in the return value."""
    from gdx_dispatch.modules.outlook import tasks
    aid, tid = uuid4(), uuid4()
    account = MagicMock(); account.id = aid; account.user_id = uuid4()
    cdb = MagicMock()
    folder_a = MagicMock(); folder_a.graph_folder_id = "fA"; folder_a.display_name = "Inbox"
    folder_b = MagicMock(); folder_b.graph_folder_id = "fB"; folder_b.display_name = "SentItems"
    tdb = MagicMock()
    tdb.get.return_value = account
    tdb.query.return_value.filter.return_value.all.return_value = [folder_a, folder_b]
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.tasks._refresh_folder_cache", return_value=(2, 0)) as refresh, \
         patch("gdx_dispatch.modules.outlook.tasks._sync_one_folder", return_value=(3, 0)) as sync_one:
        ctx.return_value.__enter__.return_value = fake_gc
        result = tasks.sync_outlook_mailbox.run(str(aid), str(tid))
    refresh.assert_called_once()
    assert sync_one.call_count == 2  # one per folder
    assert result["folders_upserted"] == 2
    assert result["folders_deleted"] == 0
    assert result["messages_upserted"] == 6  # 3 per folder × 2 folders


# ── backfill_outlook_mailbox ───────────────────────────────────────────


def test_backfill_walks_all_folders_and_filters_by_date():
    """Backfill refreshes folder cache then date-filters per folder. Skipped
    folders (Junk, Deleted) are never queried."""
    from gdx_dispatch.modules.outlook import tasks
    aid, tid = uuid4(), uuid4()
    account = MagicMock(); account.id = aid; account.user_id = uuid4()
    cdb = MagicMock()
    inbox = MagicMock(); inbox.graph_folder_id = "fInbox"; inbox.display_name = "Inbox"; inbox.well_known_name = "inbox"
    junk = MagicMock(); junk.graph_folder_id = "fJunk"; junk.display_name = "Junk"; junk.well_known_name = "junkemail"
    custom = MagicMock(); custom.graph_folder_id = "fCustom"; custom.display_name = "Receipts"; custom.well_known_name = None
    tdb = MagicMock()
    tdb.get.return_value = account
    tdb.query.return_value.filter.return_value.all.return_value = [inbox, junk, custom]
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    fake_gc = MagicMock()
    fake_gc._request.return_value.json.return_value = {"value": []}
    fake_gc.list_messages.return_value = {"@odata.deltaLink": "https://x?$deltatoken=tok"}
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.tasks._refresh_folder_cache", return_value=(3, 0)), \
         patch("gdx_dispatch.modules.outlook.tasks._persist_messages", return_value=0):
        ctx.return_value.__enter__.return_value = fake_gc
        result = tasks.backfill_outlook_mailbox.run(str(aid), str(tid), 90)

    # Each non-skipped folder triggers a date-filtered messages call.
    folder_get_calls = [
        c for c in fake_gc._request.call_args_list
        if c.args[0] == "GET" and "/messages" in c.args[1]
    ]
    paths_hit = {c.args[1] for c in folder_get_calls}
    assert "/me/mailFolders/fInbox/messages" in paths_hit
    assert "/me/mailFolders/fCustom/messages" in paths_hit
    assert "/me/mailFolders/fJunk/messages" not in paths_hit  # skipped
    # date filter present
    assert all("receivedDateTime ge " in (c.kwargs.get("params", {}).get("$filter", "")) for c in folder_get_calls)
    assert result["messages_upserted"] == 0
    assert result["folders_upserted"] == 3


# ── renew_all_outlook_subscriptions ────────────────────────────────────


def test_renew_all_renews_expiring_subscription():
    from gdx_dispatch.modules.outlook import tasks
    sub_a = MagicMock(); sub_a.id = uuid4()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.filter.return_value.all.return_value = [sub_a]

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.subscriptions.renew_subscription"):
        result = tasks.renew_all_outlook_subscriptions.run()
    assert result["examined"] == 1
    assert result["renewed"] == 1


def test_renew_all_continues_on_per_sub_failure():
    from gdx_dispatch.modules.outlook import tasks
    from gdx_dispatch.modules.outlook.subscriptions import SubscriptionError
    sub_a, sub_b = MagicMock(), MagicMock()
    sub_a.id, sub_b.id = uuid4(), uuid4()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.filter.return_value.all.return_value = [sub_a, sub_b]

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.subscriptions.renew_subscription",
               side_effect=[SubscriptionError("graph 401"), None]):
        result = tasks.renew_all_outlook_subscriptions.run()
    assert result["examined"] == 2
    assert result["renewed"] == 1
    assert result["failed"] == 1


# ── poll_outlook_mailboxes_fallback ────────────────────────────────────


def _account(connected=True):
    a = MagicMock(); a.id = uuid4()
    a.access_token_enc = "fernet" if connected else None
    return a


def _sub(expired=False, errored=False):
    s = MagicMock()
    s.expiration_at = (
        datetime.now(timezone.utc) - timedelta(hours=1) if expired
        else datetime.now(timezone.utc) + timedelta(hours=24)
    )
    s.last_error = "graph 401" if errored else None
    return s


def test_poller_skips_healthy_subscription():
    from gdx_dispatch.modules.outlook import tasks
    account = _account()
    sub = _sub()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.all.return_value = [account]
    tdb.query.return_value.filter.return_value.one_or_none.return_value = sub
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.sync_outlook_mailbox") as sync:
        result = tasks.poll_outlook_mailboxes_fallback.run()
    sync.delay.assert_not_called()
    assert result["triggered"] == 0
    assert result["skipped_healthy"] == 1


def test_poller_triggers_for_expired_subscription():
    from gdx_dispatch.modules.outlook import tasks
    account = _account()
    sub = _sub(expired=True)
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.all.return_value = [account]
    tdb.query.return_value.filter.return_value.one_or_none.return_value = sub
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.sync_outlook_mailbox") as sync:
        result = tasks.poll_outlook_mailboxes_fallback.run()
    sync.delay.assert_called_once()
    assert result["triggered"] == 1


def test_poller_triggers_when_no_subscription_row():
    from gdx_dispatch.modules.outlook import tasks
    account = _account()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.all.return_value = [account]
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.sync_outlook_mailbox") as sync:
        result = tasks.poll_outlook_mailboxes_fallback.run()
    sync.delay.assert_called_once()
    assert result["triggered"] == 1
