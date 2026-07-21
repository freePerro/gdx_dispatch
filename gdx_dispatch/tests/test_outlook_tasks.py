"""Phase 2 — Celery sync/backfill/renew/poll tasks for Outlook integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from gdx_dispatch.modules.outlook.tasks import (
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
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
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
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    _persist_messages(tdb, account, [
        {"id": "g1", "from": {"emailAddress": {"address": "doug@gdx.com"}}},
    ])
    row = tdb.add.call_args.args[0]
    assert row.direction == "outbound"


def test_persist_messages_sets_direction_inbound_when_sender_is_external():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    _persist_messages(tdb, account, [
        {"id": "g1", "from": {"emailAddress": {"address": "alice@external.com"}}},
    ])
    row = tdb.add.call_args.args[0]
    assert row.direction == "inbound"


# ── partial delta items must never blank populated rows (2026-07) ──────


def _populated_row(direction="inbound", from_address="alice@x.com"):
    from gdx_dispatch.modules.outlook.models import OutlookMessage
    row = OutlookMessage()
    row.graph_message_id = "g1"
    row.subject = "Re: garage door estimate"
    row.from_address = from_address
    row.body_preview = "Hi, following up on the quote"
    row.received_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    row.is_read = False
    row.direction = direction
    return row


def test_persist_partial_delta_item_does_not_blank_existing_row():
    """Regression — prod 2026-07: a delta resumed with a bare $deltatoken
    returns partial items (id + changed flags, no envelope). The old
    unconditional ``m.get(...)`` assignment nulled sender/subject/preview/
    received_at on every message whose read state changed, and the NULL
    received_at floated those rows to the top of /inbox (NULLs sort first
    under ORDER BY received_at DESC)."""
    tdb = MagicMock()
    row = _populated_row()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = row
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    n = _persist_messages(tdb, account, [{"id": "g1", "isRead": True}])
    assert n == 1
    assert row.subject == "Re: garage door estimate"
    assert row.from_address == "alice@x.com"
    assert row.body_preview == "Hi, following up on the quote"
    assert row.received_at == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert row.is_read is True  # the changed flag IS applied


def test_persist_partial_delta_item_does_not_reset_direction():
    tdb = MagicMock()
    row = _populated_row(direction="outbound", from_address="doug@gdx.com")
    tdb.query.return_value.filter.return_value.one_or_none.return_value = row
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    _persist_messages(tdb, account, [{"id": "g1", "isRead": True}])
    assert row.direction == "outbound"


def test_persist_new_partial_item_hydrates_via_get_message():
    """A NEW message arriving as a partial item would be born blank — the
    persist path fetches the full envelope from Graph instead."""
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    gc = MagicMock()
    gc.get_message.return_value = {
        "id": "g9", "subject": "Full subject", "bodyPreview": "preview",
        "from": {"emailAddress": {"address": "bob@x.com"}},
        "receivedDateTime": "2026-07-20T10:00:00Z", "isRead": False,
    }
    n = _persist_messages(tdb, account, [{"id": "g9", "isRead": False}], gc=gc)
    assert n == 1
    gc.get_message.assert_called_once_with("g9")
    row = tdb.add.call_args.args[0]
    assert row.subject == "Full subject"
    assert row.from_address == "bob@x.com"
    assert row.body_preview == "preview"
    assert row.received_at is not None


def test_persist_full_item_is_not_rehydrated():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    gc = MagicMock()
    _persist_messages(tdb, account, [
        {"id": "g1", "subject": "S", "from": {"emailAddress": {"address": "a@x.com"}}},
    ], gc=gc)
    gc.get_message.assert_not_called()


def test_persist_hydration_failure_still_persists_partial_row():
    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    gc = MagicMock()
    gc.get_message.side_effect = OutlookGraphAPIError(500, "boom")
    n = _persist_messages(tdb, account, [{"id": "g9", "isRead": True}], gc=gc)
    assert n == 1  # partial row beats no row; repair task can heal it later
    tdb.add.assert_called_once()


def test_persist_new_partial_item_without_gc_persists_partial():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "doug@gdx.com"
    n = _persist_messages(tdb, account, [{"id": "g9", "isRead": True}])
    assert n == 1
    row = tdb.add.call_args.args[0]
    assert row.subject is None


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
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.with_outlook_client") as ctx, \
         patch("gdx_dispatch.modules.outlook.tasks._refresh_folder_cache", return_value=(3, 0)), \
         patch("gdx_dispatch.modules.outlook.tasks._persist_messages", return_value=0):
        ctx.return_value.__enter__.return_value = fake_gc
        result = tasks.backfill_outlook_mailbox.run(str(aid), str(tid), 90)

    # The delta-prime is gone: it hit the PLAIN listing, which never returns
    # a deltaLink, so it never primed anything. Bootstrap happens in
    # _sync_one_folder on the next sync instead.
    fake_gc.list_messages.assert_not_called()

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


def _renew_tdb(expiring, subless=()):
    """Mock tenant session for renew_all: first query chain = expiring
    subscriptions, outerjoin chain = accounts with no subscription row."""
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.filter.return_value.all.return_value = list(expiring)
    (tdb.query.return_value.outerjoin.return_value
        .filter.return_value.filter.return_value.all.return_value) = list(subless)
    return tdb


def test_renew_all_renews_expiring_subscription():
    from gdx_dispatch.modules.outlook import tasks
    sub_a = MagicMock(); sub_a.id = uuid4()
    tdb = _renew_tdb([sub_a])

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.subscriptions.renew_subscription"):
        result = tasks.renew_all_outlook_subscriptions.run()
    assert result["examined"] == 1
    assert result["renewed"] == 1
    assert result["created"] == 0


def test_renew_all_continues_on_per_sub_failure():
    from gdx_dispatch.modules.outlook import tasks
    from gdx_dispatch.modules.outlook.subscriptions import SubscriptionError
    sub_a, sub_b = MagicMock(), MagicMock()
    sub_a.id, sub_b.id = uuid4(), uuid4()
    tdb = _renew_tdb([sub_a, sub_b])

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.subscriptions.renew_subscription",
               side_effect=[SubscriptionError("graph 401"), None]):
        result = tasks.renew_all_outlook_subscriptions.run()
    assert result["examined"] == 2
    assert result["renewed"] == 1
    assert result["failed"] == 1


def test_renew_all_creates_missing_subscription():
    # 2026-07-07 audit self-heal: a connected account with no subscription
    # row gets one created (nothing else ever calls create_subscription
    # once the connect-time attempt fails).
    from gdx_dispatch.modules.outlook import tasks
    account = MagicMock(); account.id = uuid4(); account.user_id = uuid4()
    tdb = _renew_tdb([], subless=[account])

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.subscriptions.create_subscription") as create:
        result = tasks.renew_all_outlook_subscriptions.run()
    assert result["created"] == 1
    assert result["create_failed"] == 0
    assert create.call_args.kwargs["user_id"] == account.user_id


def test_renew_all_create_failure_is_counted_not_fatal():
    from gdx_dispatch.modules.outlook import tasks
    from gdx_dispatch.modules.outlook.subscriptions import SubscriptionError
    account = MagicMock(); account.id = uuid4(); account.user_id = uuid4()
    tdb = _renew_tdb([], subless=[account])

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.subscriptions.create_subscription",
               side_effect=SubscriptionError("validation failed")):
        result = tasks.renew_all_outlook_subscriptions.run()
    assert result["created"] == 0
    assert result["create_failed"] == 1


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


# ── _sync_one_folder delta bootstrap (2026-07-07 audit) ────────────────


def _folder(graph_id="AAA1", name="Inbox"):
    f = MagicMock()
    f.well_known_name = None
    f.graph_folder_id = graph_id
    f.display_name = name
    return f


def _sync_state(token=None):
    s = MagicMock()
    s.full_resync_required = False
    s.delta_token = token
    return s


def test_sync_one_folder_bootstraps_delta_token_on_first_sync():
    """First sync (no stored token) must still end holding a delta link.

    The old code called gc.list_messages(), whose token-less branch hits
    the plain listing — Graph never returns @odata.deltaLink there, so no
    folder ever saved a token and every 30-minute poll re-walked the whole
    mailbox (prod: 63 sync-state rows, zero tokens)."""
    from gdx_dispatch.modules.outlook import tasks

    folder = _folder()
    state = _sync_state()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = state

    delta_link = "https://graph.microsoft.com/v1.0/x/delta?$deltatoken=tok123"
    gc = MagicMock()
    gc.list_messages_delta.return_value = {"value": [], "@odata.deltaLink": delta_link}

    account = MagicMock()
    with patch("gdx_dispatch.modules.outlook.tasks._persist_messages", return_value=0):
        upserted, removed = tasks._sync_one_folder(tdb, gc, account, folder)

    gc.list_messages_delta.assert_called_once_with(folder="AAA1", delta_token=None, top=100)
    gc.list_messages.assert_not_called()
    # The FULL deltaLink is stored — replaying the bare token drops the
    # encoded $select and Graph then returns partial (envelope-less) items.
    assert state.delta_token == delta_link
    assert state.full_resync_required is False
    assert (upserted, removed) == (0, 0)


def test_sync_one_folder_replays_stored_deltalink_verbatim():
    """A stored full deltaLink is GET verbatim (Graph contract) — never
    reduced to a bare $deltatoken, which would drop the encoded $select."""
    from gdx_dispatch.modules.outlook import tasks

    old_link = "https://graph.microsoft.com/v1.0/x/delta?$deltatoken=old"
    new_link = "https://graph.microsoft.com/v1.0/x/delta?$deltatoken=new"
    folder = _folder()
    state = _sync_state(token=old_link)
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = state

    gc = MagicMock()
    gc._request.return_value.json.return_value = {"value": [], "@odata.deltaLink": new_link}

    with patch("gdx_dispatch.modules.outlook.tasks._persist_messages", return_value=0):
        tasks._sync_one_folder(tdb, gc, MagicMock(), folder)

    gc._request.assert_called_once_with("GET", old_link)
    gc.list_messages_delta.assert_not_called()
    assert state.delta_token == new_link


def test_sync_one_folder_upgrades_legacy_bare_token_to_deltalink():
    """Pre-fix rows hold a bare token: resume with it once (tolerated), then
    store the full deltaLink so the next cycle replays the URL."""
    from gdx_dispatch.modules.outlook import tasks

    new_link = "https://graph.microsoft.com/v1.0/x/delta?$deltatoken=new"
    folder = _folder()
    state = _sync_state(token="legacy-bare-token")
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = state

    gc = MagicMock()
    gc.list_messages_delta.return_value = {"value": [], "@odata.deltaLink": new_link}

    with patch("gdx_dispatch.modules.outlook.tasks._persist_messages", return_value=0):
        tasks._sync_one_folder(tdb, gc, MagicMock(), folder)

    gc.list_messages_delta.assert_called_once_with(
        folder="AAA1", delta_token="legacy-bare-token", top=100,
    )
    assert state.delta_token == new_link


# ── repair_blank_outlook_messages ──────────────────────────────────────


def _blank_row(gid):
    from gdx_dispatch.modules.outlook.models import OutlookMessage
    row = OutlookMessage()
    row.id = uuid4()
    row.graph_message_id = gid
    return row


def _repair_tdb(account, batches, persist_target=None):
    """Mock tenant session for the repair task: self-returning query chain
    whose .all() yields `batches`; one_or_none feeds _persist_messages'
    upsert lookup so the repair actually mutates the blank row."""
    qm = MagicMock()
    qm.filter.return_value = qm
    qm.order_by.return_value = qm
    qm.limit.return_value = qm
    qm.all.side_effect = batches
    qm.one_or_none.return_value = persist_target
    qm.first.return_value = None  # OutlookSettings lookup inside _persist_messages
    tdb = MagicMock()
    tdb.get.return_value = account
    tdb.query.return_value = qm
    return tdb


def test_repair_refetches_blank_rows_and_deletes_404s():
    from gdx_dispatch.modules.outlook import tasks
    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
    aid, tid = uuid4(), uuid4()
    account = MagicMock()
    account.id = aid
    account.user_id = uuid4()
    account.upn = "doug@gdx.com"
    r1, r2 = _blank_row("g1"), _blank_row("g2")
    tdb = _repair_tdb(account, [[r1, r2], []], persist_target=r1)
    gc = MagicMock()
    gc.get_message.side_effect = [
        {"id": "g1", "subject": "Recovered", "bodyPreview": "pv",
         "from": {"emailAddress": {"address": "a@x.com"}},
         "receivedDateTime": "2026-07-01T00:00:00Z", "isRead": True},
        OutlookGraphAPIError(404, "gone"),
    ]
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = gc
        result = tasks.repair_blank_outlook_messages.run(str(aid), str(tid))
    assert result == {"repaired": 1, "deleted": 1, "errors": 0}
    tdb.delete.assert_called_once_with(r2)
    assert r1.subject == "Recovered"
    assert r1.from_address == "a@x.com"
    assert r1.received_at is not None


def test_repair_parks_unfetchable_rows_and_terminates():
    """A row Graph 500s on must not be re-selected forever — it's counted
    once and excluded from subsequent batches."""
    from gdx_dispatch.modules.outlook import tasks
    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
    aid, tid = uuid4(), uuid4()
    account = MagicMock()
    account.id = aid
    account.user_id = uuid4()
    r1 = _blank_row("g1")
    tdb = _repair_tdb(account, [[r1], []])
    gc = MagicMock()
    gc.get_message.side_effect = OutlookGraphAPIError(500, "boom")
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb), \
         patch("gdx_dispatch.modules.outlook.tasks.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = gc
        result = tasks.repair_blank_outlook_messages.run(str(aid), str(tid))
    assert result == {"repaired": 0, "deleted": 0, "errors": 1}
    tdb.delete.assert_not_called()


def test_repair_skips_when_no_account():
    from gdx_dispatch.modules.outlook import tasks
    tdb = MagicMock()
    tdb.get.return_value = None
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", return_value=tdb):
        result = tasks.repair_blank_outlook_messages.run(str(uuid4()), str(uuid4()))
    assert result["skipped"] == "no account"


# ── D3: auto-tag on persist + re-tag backfill ──────────────────────────


def test_persist_tags_new_row():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "me@x.com"
    with patch("gdx_dispatch.modules.outlook.tasks.tag_message") as tag:
        _persist_messages(tdb, account, [
            {"id": "g1", "subject": "Re: estimate",
             "from": {"emailAddress": {"address": "alice@x.com"}}},
        ])
    tag.assert_called_once()


def test_persist_does_not_tag_existing_row():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = MagicMock()
    account = MagicMock()
    account.id = uuid4()
    account.upn = "me@x.com"
    with patch("gdx_dispatch.modules.outlook.tasks.tag_message") as tag:
        _persist_messages(tdb, account, [{"id": "g1", "subject": "updated"}])
    tag.assert_not_called()


def test_persist_tag_failure_does_not_break_upsert():
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    account = MagicMock()
    account.id = uuid4()
    account.upn = "me@x.com"
    with patch("gdx_dispatch.modules.outlook.tasks.tag_message", side_effect=RuntimeError("boom")):
        n = _persist_messages(tdb, account, [
            {"id": "g1", "subject": "x", "from": {"emailAddress": {"address": "a@x.com"}}},
        ])
    assert n == 1  # upsert survives a tagging failure


def test_retag_untagged_walks_and_commits():
    from gdx_dispatch.modules.outlook.tasks import _retag_untagged
    r1, r2 = MagicMock(), MagicMock()
    r1.id, r2.id = uuid4(), uuid4()
    qm = MagicMock()
    qm.filter.return_value = qm
    qm.order_by.return_value = qm
    qm.limit.return_value = qm
    qm.all.side_effect = [[r1, r2], []]  # one batch, then drained
    tdb = MagicMock()
    tdb.query.return_value = qm
    with patch("gdx_dispatch.modules.outlook.tasks.tag_message", side_effect=[True, False]) as tag:
        out = _retag_untagged(tdb, batch=200)
    assert out == {"scanned": 2, "tagged": 1}
    assert tag.call_count == 2
    tdb.commit.assert_called()


# ── sweep_vendor_bill_history (Phase 2, D3) ────────────────────────────
#
# Real in-memory SQLite (shared StaticPool connection, so the task's three
# SessionLocal() sessions see one database): the checkpoint + repeatability
# semantics are exactly what MagicMock query chains can't prove.


def _sweep_env(allowlist=("midwest.com",)):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookSettings

    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(eng)
    maker = sessionmaker(bind=eng)
    db = maker()
    account = OutlookAccount(user_id=str(uuid4()))
    db.add(account)
    db.add(OutlookSettings(id=1, vendor_bill_sender_allowlist=list(allowlist)))
    db.commit()
    account_id = account.id
    db.close()
    return maker, account_id


def _mirror_msg(maker, account_id, gid, sender, *, has_att=True, days_ago=1, stamped=False):
    from gdx_dispatch.modules.outlook.models import OutlookMessage

    db = maker()
    db.add(OutlookMessage(
        account_id=account_id,
        graph_message_id=gid,
        from_address=sender,
        has_attachments=has_att,
        received_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        vendor_bills_ingested_at=datetime.now(timezone.utc) if stamped else None,
    ))
    db.commit()
    db.close()


def _stamps(maker, account_id):
    """{graph_message_id: bool(checkpointed)} for every mirror row."""
    from gdx_dispatch.modules.outlook.models import OutlookMessage

    db = maker()
    try:
        rows = db.query(OutlookMessage).filter(OutlookMessage.account_id == account_id).all()
        return {r.graph_message_id: r.vendor_bills_ingested_at is not None for r in rows}
    finally:
        db.close()


class _SweepGC:
    """PDF attachments per message (default 1, override via counts); records downloads."""

    def __init__(self, counts=None):
        self.downloads = []
        self._counts = counts or {}

    def list_attachments(self, msg_id):
        return [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": f"att-{msg_id}-{i}",
            "contentType": "application/pdf",
            "name": f"{msg_id}-{i}.pdf",
        } for i in range(self._counts.get(msg_id, 1))]

    def download_attachment(self, msg_id, att_id):
        self.downloads.append(att_id)
        return b"%PDF-1.4 " + att_id.encode()


def _run_sweep(maker, account_id, gc, monkeypatch, *, days=365, upload=None):
    from types import SimpleNamespace

    from gdx_dispatch.modules.outlook import tasks

    monkeypatch.setattr(
        "gdx_dispatch.modules.outlook.vendor_bill_ingest.upload_midwest_invoice",
        upload or (lambda *a, **k: SimpleNamespace(created=True)),
    )
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", side_effect=lambda: maker()), \
         patch("gdx_dispatch.modules.outlook.tasks.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = gc
        return tasks.sweep_vendor_bill_history.run(str(account_id), str(uuid4()), days=days)


def test_sweep_filters_candidates_ingests_and_checkpoints(monkeypatch):
    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-bill", "billing@midwest.com", days_ago=10)
    _mirror_msg(maker, aid, "m-stranger", "x@stranger.com", days_ago=10)
    _mirror_msg(maker, aid, "m-noatt", "billing@midwest.com", has_att=False, days_ago=10)
    _mirror_msg(maker, aid, "m-done", "billing@midwest.com", days_ago=10, stamped=True)
    _mirror_msg(maker, aid, "m-ancient", "billing@midwest.com", days_ago=400)

    gc = _SweepGC()
    result = _run_sweep(maker, aid, gc, monkeypatch)

    assert result["candidates"] == 1
    assert result["ingested"] == 1
    assert result["downloads"] == 1
    assert result["cap_hit"] is False
    assert gc.downloads == ["att-m-bill-0"]
    stamps = _stamps(maker, aid)
    assert stamps["m-bill"] is True          # processed → checkpointed
    assert stamps["m-stranger"] is False     # never a candidate
    assert stamps["m-ancient"] is False      # outside the window
    # 400-day-old mirror row exists, so the 365-day window IS fully mirrored.
    assert result["window_covered"] is True

    # Repeatability: a second run finds nothing left to do.
    gc2 = _SweepGC()
    result2 = _run_sweep(maker, aid, gc2, monkeypatch)
    assert result2["candidates"] == 0
    assert gc2.downloads == []


def test_sweep_noop_when_allowlist_empty(monkeypatch):
    maker, aid = _sweep_env(allowlist=())
    _mirror_msg(maker, aid, "m1", "billing@midwest.com")
    result = _run_sweep(maker, aid, _SweepGC(), monkeypatch)
    assert result == {"skipped": "allowlist empty"}


def test_sweep_download_budget_stops_run_and_next_run_resumes(monkeypatch):
    from gdx_dispatch.modules.outlook import tasks

    maker, aid = _sweep_env()
    for i in range(3):
        _mirror_msg(maker, aid, f"m{i}", "billing@midwest.com", days_ago=i + 1)
    monkeypatch.setattr(tasks, "SWEEP_MAX_DOWNLOADS_PER_RUN", 2)

    gc = _SweepGC()
    result = _run_sweep(maker, aid, gc, monkeypatch)
    assert result["downloads"] == 2
    assert result["ingested"] == 2
    assert result["skipped_no_budget"] == 1
    assert result["cap_hit"] is True
    assert sum(_stamps(maker, aid).values()) == 2  # only the processed two

    # The next run picks up exactly the remainder.
    gc2 = _SweepGC()
    result2 = _run_sweep(maker, aid, gc2, monkeypatch)
    assert result2["candidates"] == 1
    assert result2["downloads"] == 1
    assert result2["cap_hit"] is False
    assert sum(_stamps(maker, aid).values()) == 3


def test_sweep_error_leaves_message_unstamped_for_retry(monkeypatch):
    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError

    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-err", "billing@midwest.com")

    class _BrokenGC(_SweepGC):
        def list_attachments(self, msg_id):
            raise OutlookGraphAPIError(500, "boom")

    result = _run_sweep(maker, aid, _BrokenGC(), monkeypatch)
    assert result["errors"] == 1
    assert result["ingested"] == 0
    assert _stamps(maker, aid)["m-err"] is False  # retryable next run


def test_sweep_skips_when_no_account(monkeypatch):
    maker, _aid = _sweep_env()
    from gdx_dispatch.modules.outlook import tasks

    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", side_effect=lambda: maker()):
        result = tasks.sweep_vendor_bill_history.run(str(uuid4()), str(uuid4()))
    assert result == {"skipped": "no account"}


def test_sweep_multi_pdf_message_cut_mid_run_is_not_stamped_and_resumes(monkeypatch):
    """The load-bearing checkpoint rule, end-to-end: a message whose PDF set is
    cut short by the RUN budget (not a full-budget overflow) must NOT be
    checkpointed — the next, bigger run completes it."""
    from gdx_dispatch.modules.outlook import tasks

    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-small", "billing@midwest.com", days_ago=1)
    _mirror_msg(maker, aid, "m-big", "billing@midwest.com", days_ago=2)
    counts = {"m-small": 1, "m-big": 3}

    monkeypatch.setattr(tasks, "SWEEP_MAX_DOWNLOADS_PER_RUN", 2)
    gc = _SweepGC(counts)
    result = _run_sweep(maker, aid, gc, monkeypatch)
    # m-small (newest) used 1; m-big started with remaining=1 < full budget →
    # cut short, retryable, NOT quarantined, NOT stamped.
    assert result["downloads"] == 2
    assert result["capped"] == 1
    assert result["quarantined"] == 0
    assert result["cap_hit"] is True
    stamps = _stamps(maker, aid)
    assert stamps["m-small"] is True
    assert stamps["m-big"] is False

    monkeypatch.setattr(tasks, "SWEEP_MAX_DOWNLOADS_PER_RUN", 5)
    gc2 = _SweepGC(counts)
    result2 = _run_sweep(maker, aid, gc2, monkeypatch)
    assert result2["candidates"] == 1
    assert result2["downloads"] == 3
    assert result2["ingested"] == 3
    assert _stamps(maker, aid)["m-big"] is True


def test_sweep_quarantines_message_bigger_than_entire_budget(monkeypatch):
    """A message needing more downloads than the WHOLE per-run budget can never
    complete — it must be parked (checkpointed incomplete), not allowed to eat
    every future run's budget from the head of the queue."""
    from gdx_dispatch.modules.outlook import tasks

    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-monster", "billing@midwest.com")
    monkeypatch.setattr(tasks, "SWEEP_MAX_DOWNLOADS_PER_RUN", 2)

    gc = _SweepGC({"m-monster": 3})
    result = _run_sweep(maker, aid, gc, monkeypatch)
    assert result["downloads"] == 2
    assert result["quarantined"] == 1
    assert _stamps(maker, aid)["m-monster"] is True  # parked

    # It does NOT come back next run.
    gc2 = _SweepGC({"m-monster": 3})
    assert _run_sweep(maker, aid, gc2, monkeypatch)["candidates"] == 0


def test_ingest_helper_skips_already_stamped_candidates(monkeypatch):
    """The delta path re-surfaces a message on every isRead/flag change; the
    checkpoint must be READ there, not just written — no re-download."""
    from gdx_dispatch.modules.outlook import tasks

    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-done", "billing@midwest.com", stamped=True)
    gc = _SweepGC()
    cand = [{"id": "m-done", "hasAttachments": True,
             "from": {"emailAddress": {"address": "billing@midwest.com"}}}]
    with patch("gdx_dispatch.modules.outlook.tasks.SessionLocal", side_effect=lambda: maker()):
        totals = tasks._ingest_vendor_bills(gc, cand, ["midwest.com"], stamp_account_id=aid)
    assert totals["skipped_already_ingested"] == 1
    assert totals["downloads"] == 0
    assert gc.downloads == []


def test_sweep_reports_uncovered_window_when_mirror_is_shallow(monkeypatch):
    """backfill_days defaults to 90; a 365-day sweep over a 90-day mirror must
    say so — cap_hit:false alone must never read as 'the whole year is in'."""
    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m1", "billing@midwest.com", days_ago=10)
    result = _run_sweep(maker, aid, _SweepGC(), monkeypatch, days=365)
    assert result["window_covered"] is False
    assert result["mirror_oldest"] is not None


# ── LLM rung threading through the sweep (D4) ──────────────────────────


def _llm_env(monkeypatch, *, client="dummy", midwest_fails=True):
    """Patch the parser to reject, the LLM upload to succeed, and get_client."""
    from types import SimpleNamespace

    from gdx_dispatch.core.llm.anthropic_client import LLMNotConfigured

    if midwest_fails:
        from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
            MidwestInvoiceParseError,
        )

        def _fail(*a, **k):
            raise MidwestInvoiceParseError("nope")

        monkeypatch.setattr(
            "gdx_dispatch.modules.outlook.vendor_bill_ingest.upload_midwest_invoice", _fail,
        )
    monkeypatch.setattr(
        "gdx_dispatch.modules.outlook.vendor_bill_ingest.upload_invoice_via_llm",
        lambda *a, **k: SimpleNamespace(created=True),
    )
    if client is None:
        def _no_key(db, tid):
            raise LLMNotConfigured("no key")
        monkeypatch.setattr("gdx_dispatch.core.llm.anthropic_client.get_client", _no_key)
    else:
        monkeypatch.setattr(
            "gdx_dispatch.core.llm.anthropic_client.get_client", lambda db, tid: object(),
        )


def test_sweep_llm_rung_ingests_and_stamps(monkeypatch):
    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-scan", "billing@midwest.com")
    _llm_env(monkeypatch)

    result = _run_sweep(maker, aid, _SweepGC(), monkeypatch,
                        upload=_raise_midwest_parse_error)
    assert result["ingested_llm"] == 1
    assert result["llm_extractions"] == 1
    assert result["unparseable"] == 0
    assert _stamps(maker, aid)["m-scan"] is True


def test_sweep_llm_ceiling_blocks_stamp_and_next_run_finishes(monkeypatch):
    from gdx_dispatch.modules.outlook import tasks

    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-new", "billing@midwest.com", days_ago=1)
    _mirror_msg(maker, aid, "m-old", "billing@midwest.com", days_ago=2)
    _llm_env(monkeypatch)
    monkeypatch.setattr(tasks, "LLM_MAX_EXTRACTIONS_PER_RUN", 1)

    result = _run_sweep(maker, aid, _SweepGC(), monkeypatch,
                        upload=_raise_midwest_parse_error)
    assert result["ingested_llm"] == 1
    assert result["llm_capped"] == 1
    assert result["cap_hit"] is True        # the ceiling must say "run again"
    stamps = _stamps(maker, aid)
    assert stamps["m-new"] is True
    assert stamps["m-old"] is False         # ceiling-cut → retried later

    result2 = _run_sweep(maker, aid, _SweepGC(), monkeypatch,
                         upload=_raise_midwest_parse_error)
    assert result2["candidates"] == 1
    assert result2["ingested_llm"] == 1
    assert result2["cap_hit"] is False
    assert _stamps(maker, aid)["m-old"] is True


def test_sweep_without_llm_key_stamps_unparseable_as_before(monkeypatch):
    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-scan", "billing@midwest.com")
    _llm_env(monkeypatch, client=None)

    result = _run_sweep(maker, aid, _SweepGC(), monkeypatch,
                        upload=_raise_midwest_parse_error)
    assert result["unparseable"] == 1
    assert result["llm_extractions"] == 0
    # Feature-off semantics: processed to current capability → checkpointed.
    # (Re-arm with UPDATE ... SET vendor_bills_ingested_at = NULL after
    # configuring a key, as documented in the D3 commit.)
    assert _stamps(maker, aid)["m-scan"] is True


def _raise_midwest_parse_error(*a, **k):
    from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
        MidwestInvoiceParseError,
    )
    raise MidwestInvoiceParseError("nope")


def test_sweep_broken_llm_key_leaves_messages_unstamped(monkeypatch):
    """A key-rotation incident (LLMKeyStorageError) must NOT stamp scans as
    processed — they wait, retryable, until the key is fixed."""
    from gdx_dispatch.core.llm.key_storage import LLMKeyStorageError

    maker, aid = _sweep_env()
    _mirror_msg(maker, aid, "m-scan", "billing@midwest.com")
    _llm_env(monkeypatch)  # sets up parser-fails + llm-succeeds

    def _broken(db, tid):
        raise LLMKeyStorageError("cannot decrypt with current GDX_FERNET_KEY")

    monkeypatch.setattr("gdx_dispatch.core.llm.anthropic_client.get_client", _broken)
    result = _run_sweep(maker, aid, _SweepGC(), monkeypatch,
                        upload=_raise_midwest_parse_error)
    assert result["errors"] == 1
    assert result["llm_extractions"] == 0
    assert result["unparseable"] == 0
    assert _stamps(maker, aid)["m-scan"] is False   # retried after the fix
