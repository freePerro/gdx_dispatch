"""Sprint Outlook Integration — Celery sync + lifecycle tasks.

Four tasks ship in this module:
- ``sync_outlook_mailbox(account_id, tenant_id)``: incremental delta sync,
  triggered by webhook events (slice S13) or by the fallback poller (S17).
- ``backfill_outlook_mailbox(account_id, tenant_id, days)``: initial pull
  on connect (called from /callback success), filtered by receivedDateTime.
- ``renew_all_outlook_subscriptions()``: beat task — runs every 6h, renews
  any subscription nearing expiry (within RENEWAL_THRESHOLD_HOURS = 12h).
- ``poll_outlook_mailboxes_fallback()``: beat task — runs every 15min,
  triggers sync for any connected mailbox whose subscription is missing,
  expired, or errored. Safety net for webhook drops.
- ``repair_blank_outlook_messages(account_id, tenant_id)``: one-shot manual
  repair for rows blanked by the 2026-07 partial-delta overwrite bug.
- ``sweep_vendor_bill_history(account_id, tenant_id, days)``: repeatable,
  admin-triggered vendor-bill history sweep over the LOCAL message mirror —
  downloads allowlisted senders' PDF attachments (bounded per run) and feeds
  the vendor-invoice pipeline. Checkpointed per message, so re-running only
  processes what previous runs didn't reach.

Body persistence to R2 is deferred to a future slice (the column
``body_r2_key`` is set; actual write is a no-op until R2 client lands).
"""
from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.models import (
    OutlookAccount,
    OutlookFolder,
    OutlookFolderPrefs,
    OutlookFolderSyncState,
    OutlookMessage,
    OutlookSettings,
    OutlookSubscription,
)
from gdx_dispatch.modules.outlook.tagger import tag_message
from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired, with_outlook_client
from gdx_dispatch.modules.outlook.vendor_bill_ingest import (
    ingest_message_attachments,
    is_candidate,
    new_totals,
    normalize_allowlist,
    sender_allowed,
)

log = logging.getLogger("gdx_dispatch.modules.outlook.tasks")

# Per-backfill-folder page cap (D5). The incremental delta sync is bounded by
# Graph's delta paging, not a count; only the initial backfill needs a cap so a
# giant mailbox can't walk forever. (A prior MAX_MESSAGES_PER_RUN=500 constant
# was defined but never referenced — removed to stop implying an unenforced
# limit.)
BACKFILL_MAX_MESSAGES_PER_RUN = 5000

# Vendor-bill history sweep bounds (Phase 2, increment D3). Both are per-RUN
# budgets, not coverage limits — the per-message checkpoint makes repeat runs
# pick up exactly where the last one stopped. Messages bounds Graph
# list_attachments calls; downloads bounds attachment-content fetches (the
# expensive, throttle-prone call — design cap from [AUDIT-R3]).
SWEEP_MAX_MESSAGES_PER_RUN = 500
SWEEP_MAX_DOWNLOADS_PER_RUN = 50

# Folders we cache + show in the rail but do NOT actively sync messages from.
# Live-fetched on click via the views_router live-fetch path. Per Doug's
# 2026-04-29 directive: "just show the folder and fetch live on click" for
# Junk + Deleted. Outbox is transient by Microsoft's design; recoverable
# items are infrastructure.
SKIP_SYNC_WELL_KNOWN = {
    "junkemail",
    "deleteditems",
    "outbox",
    "recoverableitemsdeletions",
    "recoverableitemspurges",
    "recoverableitemsversions",
    "syncissues",
    "conflicts",
    "localfailures",
    "serverfailures",
}

MAX_FOLDER_DEPTH = 5
MAX_FOLDERS_PER_ACCOUNT = 500


def _open_tenant_session(tenant_id: UUID, db_url: str) -> Session:
    return SessionLocal()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.debug("could not parse Microsoft Graph ISO datetime: %r", value)
        return None


# Envelope keys whose collective ABSENCE marks a partial delta item (a resume
# with a bare $deltatoken carries no $select, so changed messages come back as
# id + changed flags only). A full $select response always has these keys.
_ENVELOPE_KEYS = ("subject", "from", "bodyPreview", "receivedDateTime")


def _is_partial_item(m: dict[str, Any]) -> bool:
    return not any(k in m for k in _ENVELOPE_KEYS)


def _persist_messages(
    tdb: Session,
    account: OutlookAccount,
    graph_messages: list[dict[str, Any]],
    *,
    folder_id: str | None = None,
    folder_display_name: str | None = None,
    gc=None,
) -> int:
    """Upsert OutlookMessage rows from a Graph response. Returns count touched.

    folder_id / folder_display_name are stamped on each row so the SPA can
    filter by folder. Pass None when the call site is folder-agnostic
    (legacy callers pre-folder-sync); rows from such calls inherit whatever
    folder_id the upsert path defaulted to.

    A field is only written when its key is PRESENT in the Graph payload.
    Delta pages can carry partial items (id + changed flags, no envelope);
    the pre-2026-07 unconditional ``m.get(...)`` assignment blanked
    sender/subject/preview/received_at on every message whose read state
    changed, and the NULL received_at then floated those rows to the top of
    /inbox (NULLs sort first under ORDER BY received_at DESC).
    """
    upserted = 0
    # Load tag settings ONCE per batch, not once per message (D3 auto-tag runs
    # inside this loop; the per-message OutlookSettings query was an N+1 on a
    # large backfill folder).
    tag_settings = tdb.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    for m in graph_messages:
        graph_id = m.get("id")
        if not graph_id:
            continue
        existing = (
            tdb.query(OutlookMessage)
            .filter(
                OutlookMessage.account_id == account.id,
                OutlookMessage.graph_message_id == graph_id,
            )
            .one_or_none()
        )
        is_new = existing is None
        if is_new and gc is not None and _is_partial_item(m):
            # A NEW message arriving as a partial item would be born blank —
            # fetch the full envelope instead. Existing rows don't need this:
            # key-gating below already preserves their populated fields.
            try:
                m = gc.get_message(graph_id)
            except OutlookGraphAPIError as exc:
                log.warning(
                    "hydration failed for graph_id=%s — persisting partial row: %s",
                    graph_id, exc,
                )
        if existing is None:
            row = OutlookMessage()
            row.account_id = account.id
            row.graph_message_id = graph_id
            tdb.add(row)
        else:
            row = existing
        if "internetMessageId" in m:
            row.internet_message_id = m["internetMessageId"]
        if "conversationId" in m:
            row.conversation_id = m["conversationId"]
        if "subject" in m:
            row.subject = m["subject"]
        if "from" in m:
            from_field = (m["from"] or {}).get("emailAddress") or {}
            row.from_address = from_field.get("address")
        if "toRecipients" in m:
            row.to_addresses = [
                r.get("emailAddress", {}).get("address")
                for r in (m["toRecipients"] or [])
            ]
        if "ccRecipients" in m:
            row.cc_addresses = [
                r.get("emailAddress", {}).get("address")
                for r in (m["ccRecipients"] or [])
            ]
        if "bccRecipients" in m:
            row.bcc_addresses = [
                r.get("emailAddress", {}).get("address")
                for r in (m["bccRecipients"] or [])
            ]
        if "bodyPreview" in m:
            row.body_preview = m["bodyPreview"]
        if "hasAttachments" in m:
            row.has_attachments = bool(m["hasAttachments"])
        if "isRead" in m:
            row.is_read = bool(m["isRead"])
        # in_reply_to: Graph doesn't expose this as a selectable property;
        # threading is via conversation_id. Leave NULL until/unless the
        # singleValueExtendedProperties expansion is wired.
        if "receivedDateTime" in m:
            row.received_at = _parse_iso(m["receivedDateTime"])
        if "sentDateTime" in m:
            row.sent_at = _parse_iso(m["sentDateTime"])
        # direction: outbound when sender == mailbox owner; inbound otherwise.
        # Explicit derivation prevents sent-folder syncs from being mislabeled
        # as inbound — but only when this payload carries sender info, else a
        # partial item would reset outbound rows to the inbound default.
        if is_new or "from" in m:
            if account.upn and row.from_address and account.upn.lower() == row.from_address.lower():
                row.direction = "outbound"
            else:
                row.direction = "inbound"
        body = m.get("body") or {}
        if body.get("content"):
            # NOTE: body_r2_key is set here but no R2 upload code exists in
            # this module. Until that lands, do NOT claim a key — leaving
            # NULL means /inbox renders body_preview, which is correct
            # behavior. Setting body_r2_key without an actual R2 upload
            # would 404 in any code path that fetches the blob.
            row.body_size_bytes = len(body["content"])
        # Folder stamping. Source of truth: caller's folder context.
        if folder_id is not None:
            row.folder_id = folder_id
        if folder_display_name is not None:
            row.folder_display_name = folder_display_name

        # D3 — auto-tag newly-synced mail so the by-customer/by-job tabs
        # actually fill. Only NEW rows (tag_message self-skips already-tagged
        # ones anyway); runs the cheap tenant-DB strategies (auto_match email
        # match + job_thread subject regex). The AI strategy needs control_db
        # and is a stub — deliberately not wired through the sync stack here.
        # Tagging MUST NOT break the upsert: a lookup failure logs + continues.
        if is_new:
            try:
                tag_message(row, tdb, settings=tag_settings)
            except Exception:  # noqa: BLE001
                log.exception("auto-tag failed for graph_id=%s (upsert kept)", graph_id)

        upserted += 1
    return upserted


# ── folder cache + per-folder delta sync ──────────────────────────────


def _refresh_folder_cache(tdb: Session, gc, account: OutlookAccount) -> tuple[int, int]:
    """Walk Microsoft Graph mailFolders tree (capped at MAX_FOLDER_DEPTH +
    MAX_FOLDERS_PER_ACCOUNT), upsert OutlookFolder rows, cascade-delete folders
    that no longer exist in Graph (taking their messages, prefs, and
    sync_state with them).

    Returns (upserted, deleted).
    """
    folders = gc.list_all_folders(
        max_depth=MAX_FOLDER_DEPTH,
        max_total=MAX_FOLDERS_PER_ACCOUNT,
        include_hidden=False,
    )
    seen_ids: set[str] = set()
    upserted = 0
    for f in folders:
        graph_id = f.get("id")
        if not graph_id:
            continue
        seen_ids.add(graph_id)
        existing = (
            tdb.query(OutlookFolder)
            .filter(
                OutlookFolder.account_id == account.id,
                OutlookFolder.graph_folder_id == graph_id,
            )
            .one_or_none()
        )
        if existing is None:
            row = OutlookFolder()
            row.account_id = account.id
            row.graph_folder_id = graph_id
            tdb.add(row)
        else:
            row = existing
        row.display_name = f.get("displayName") or "(unnamed)"
        row.parent_folder_id = f.get("parentFolderId")
        row.well_known_name = (f.get("wellKnownName") or None) and str(f["wellKnownName"]).lower()
        row.total_count = int(f.get("totalItemCount") or 0)
        row.unread_count = int(f.get("unreadItemCount") or 0)
        row.child_folder_count = int(f.get("childFolderCount") or 0)
        row.is_hidden = bool(f.get("isHidden"))
        row.depth = int(f.get("depth") or 0)
        row.last_seen_at = datetime.now(timezone.utc)
        upserted += 1

    deleted = 0
    if seen_ids:
        stale = (
            tdb.query(OutlookFolder)
            .filter(
                OutlookFolder.account_id == account.id,
                OutlookFolder.graph_folder_id.notin_(seen_ids),
            )
            .all()
        )
        for s in stale:
            # Cascade: messages in this folder, prefs, sync_state.
            tdb.query(OutlookMessage).filter(
                OutlookMessage.account_id == account.id,
                OutlookMessage.folder_id == s.graph_folder_id,
            ).delete(synchronize_session=False)
            tdb.query(OutlookFolderSyncState).filter(
                OutlookFolderSyncState.account_id == account.id,
                OutlookFolderSyncState.folder_id == s.graph_folder_id,
            ).delete(synchronize_session=False)
            tdb.query(OutlookFolderPrefs).filter(
                OutlookFolderPrefs.account_id == account.id,
                OutlookFolderPrefs.folder_id == s.graph_folder_id,
            ).delete(synchronize_session=False)
            tdb.delete(s)
            deleted += 1
    return upserted, deleted


def _sync_one_folder(
    tdb: Session,
    gc,
    account: OutlookAccount,
    folder: OutlookFolder,
    *,
    allowlist: list[str] | None = None,
    candidates: list[dict] | None = None,
) -> tuple[int, int]:
    """Delta-sync one folder. Returns (upserted, removed).

    Skips folders in SKIP_SYNC_WELL_KNOWN (Junk, Deleted, etc.). On 410 Gone
    (delta token expired), drops the token + sets full_resync_required so
    the next call walks fresh.
    """
    if folder.well_known_name and folder.well_known_name in SKIP_SYNC_WELL_KNOWN:
        return 0, 0
    state = (
        tdb.query(OutlookFolderSyncState)
        .filter(
            OutlookFolderSyncState.account_id == account.id,
            OutlookFolderSyncState.folder_id == folder.graph_folder_id,
        )
        .one_or_none()
    )
    if state is None:
        state = OutlookFolderSyncState()
        state.account_id = account.id
        state.folder_id = folder.graph_folder_id
        tdb.add(state)
        tdb.flush()

    stored = None if state.full_resync_required else state.delta_token
    upserted = removed = 0
    next_link: str | None = None
    last_resp: dict[str, Any] = {}
    try:
        while True:
            if next_link:
                last_resp = gc._request("GET", next_link).json()
            elif stored and stored.startswith("http"):
                # Stored value is the FULL deltaLink from the previous sync.
                # Graph's contract is to replay it verbatim — the URL carries
                # the encoded $select, so changed messages come back with
                # their envelope fields instead of as bare partial items.
                last_resp = gc._request("GET", stored).json()
            else:
                # No state (bootstrap / full resync) or a legacy bare token
                # from before deltaLinks were stored whole. The bare-token
                # resume drops $select — _persist_messages tolerates the
                # resulting partial items, and the deltaLink stored below
                # upgrades this folder to full-URL replay from now on.
                #
                # list_messages_delta, not list_messages: the plain listing
                # never returns a deltaLink, so a token could never
                # bootstrap and every sync walked the whole mailbox.
                last_resp = gc.list_messages_delta(
                    folder=folder.graph_folder_id,
                    delta_token=stored,
                    top=100,
                )
            page_msgs = last_resp.get("value") or []
            removed += _apply_message_deletes(tdb, account, page_msgs)
            non_removed = [m for m in page_msgs if not m.get("@removed")]
            upserted += _persist_messages(
                tdb, account, non_removed,
                folder_id=folder.graph_folder_id,
                folder_display_name=folder.display_name,
                gc=gc,
            )
            # Vendor-bill auto-ingest: only COLLECT candidates here (cheap, no
            # Graph call). The actual download + pipeline runs AFTER this folder's
            # sync state commits, in an isolated session — see _ingest_vendor_bills.
            if allowlist and candidates is not None:
                candidates.extend(m for m in non_removed if is_candidate(m, allowlist))
            next_link = last_resp.get("@odata.nextLink")
            if not next_link:
                break
        delta_link = last_resp.get("@odata.deltaLink")
        if delta_link:
            # Store the WHOLE deltaLink, not the extracted $deltatoken. The
            # bare token loses the encoded $select; resuming with it made
            # Graph return partial items, whose absent fields the old persist
            # path then wrote over good rows as NULLs (prod 2026-07: blank
            # sender/subject/preview at the top of /inbox).
            state.delta_token = delta_link
        state.last_sync_at = datetime.now(timezone.utc)
        state.last_error = None
        state.full_resync_required = False
    except OutlookGraphAPIError as exc:
        if exc.status_code == 410:
            log.warning(
                "delta token expired for folder %s (%s) — full resync queued",
                folder.display_name, folder.graph_folder_id,
            )
            state.delta_token = None
            state.full_resync_required = True
            state.last_error = f"410 Gone — token expired: {str(exc)[:200]}"
        else:
            state.last_error = str(exc)[:500]
            raise
    return upserted, removed


def _ingest_vendor_bills(
    gc,
    candidates: list[dict],
    allowlist: list[str],
    *,
    stamp_account_id: UUID | None = None,
    max_downloads: int | None = None,
) -> dict[str, int]:
    """Download + pipeline the collected vendor-bill candidates in a session
    fully isolated from the sync's tenant session. Each message commits on its
    own, and a poisoned session is discarded + replaced — so the pipeline's
    flush/rollback/disk-I/O can never reach the sync mirror, and one bad bill
    can't abort the whole sync.

    ``stamp_account_id``: when given, each cleanly-processed message (no
    errors, not budget-cut) has its mirror row checkpointed
    (``vendor_bills_ingested_at``) so the history sweep never re-downloads it —
    and candidates whose mirror row is ALREADY checkpointed are skipped without
    any Graph call (the delta sync re-surfaces a message on every isRead/flag/
    move change; without this read-side check the hot path would re-download
    its PDFs each time).
    ``max_downloads``: shared download budget across all candidates; the ones
    left unattempted when it runs out are counted in ``skipped_no_budget`` and
    stay un-checkpointed for the next run. A message that gets budget-cut while
    holding the ENTIRE run budget can never complete under this budget — it is
    checkpointed anyway and counted in ``quarantined`` (loudly logged) so it
    can't starve every later run.
    """
    totals = new_totals()
    totals["skipped_no_budget"] = 0
    totals["skipped_already_ingested"] = 0
    totals["quarantined"] = 0
    if not candidates or not allowlist:
        return totals
    remaining = max_downloads
    idb = SessionLocal()
    try:
        already_stamped: set[str] = set()
        if stamp_account_id is not None:
            ids = [m["id"] for m in candidates if m.get("id")]
            if ids:
                already_stamped = {
                    gid for (gid,) in idb.query(OutlookMessage.graph_message_id)
                    .filter(
                        OutlookMessage.account_id == stamp_account_id,
                        OutlookMessage.graph_message_id.in_(ids),
                        OutlookMessage.vendor_bills_ingested_at.isnot(None),
                    )
                    .all()
                }
        for i, m in enumerate(candidates):
            if m.get("id") in already_stamped:
                totals["skipped_already_ingested"] += 1
                continue
            if remaining is not None and remaining <= 0:
                totals["skipped_no_budget"] = len(candidates) - i
                break
            had_full_budget = remaining is not None and remaining == max_downloads
            try:
                r = ingest_message_attachments(idb, gc, m, allowlist, max_downloads=remaining)
                if remaining is not None:
                    remaining -= r["downloads"]
                clean = r["errors"] == 0 and r["capped"] == 0
                # A message the FULL budget couldn't finish will never finish:
                # park it (checkpoint + loud log) instead of letting it eat
                # every future run's budget from the head of the queue.
                quarantine = bool(r["capped"]) and had_full_budget
                if quarantine:
                    totals["quarantined"] += 1
                    log.warning(
                        "vendor_bill_ingest: message %s needs more downloads than the "
                        "entire per-run budget (%s) — quarantined (checkpointed incomplete)",
                        m.get("id"), max_downloads,
                    )
                if stamp_account_id is not None and m.get("id") and (clean or quarantine):
                    idb.query(OutlookMessage).filter(
                        OutlookMessage.account_id == stamp_account_id,
                        OutlookMessage.graph_message_id == m["id"],
                    ).update(
                        {"vendor_bills_ingested_at": datetime.now(timezone.utc)},
                        synchronize_session=False,
                    )
                idb.commit()
                for k, v in r.items():
                    totals[k] = totals.get(k, 0) + v
            except Exception:  # noqa: BLE001
                log.exception("vendor_bill_ingest: message %s failed", m.get("id"))
                totals["errors"] += 1
                if remaining is not None:
                    # A non-Graph exception (timeout, poisoned session) can fire
                    # mid-download; its Graph spend is unaccounted. Charge at
                    # least one download so a flaky run can't blow past the cap.
                    remaining -= 1
                with contextlib.suppress(Exception):
                    idb.rollback()
                idb.close()
                idb = SessionLocal()  # fresh session in case the last one poisoned
    finally:
        idb.close()
    return totals


def _apply_message_deletes(
    tdb: Session,
    account: OutlookAccount,
    msgs: list[dict[str, Any]],
) -> int:
    """Process @removed annotations from a delta page. Returns delete count."""
    deleted = 0
    for m in msgs:
        if not m.get("@removed"):
            continue
        graph_id = m.get("id")
        if not graph_id:
            continue
        n = (
            tdb.query(OutlookMessage)
            .filter(
                OutlookMessage.account_id == account.id,
                OutlookMessage.graph_message_id == graph_id,
            )
            .delete(synchronize_session=False)
        )
        deleted += n
    return deleted


# ── tasks ──────────────────────────────────────────────────────────────


@celery_app.task(name="outlook.sync_outlook_mailbox", bind=True)
def sync_outlook_mailbox(self, account_id: str, tenant_id: str) -> dict:
    """Incremental delta sync across all synced folders.

    Per-folder delta tokens live in OutlookFolderSyncState. Returns
    {folders_upserted, folders_deleted, messages_upserted, messages_removed,
    failed_folders}.
    """
    aid = UUID(account_id)
    tid = UUID(tenant_id)
    with contextlib.closing(SessionLocal()) as tdb, \
         contextlib.closing(SessionLocal()) as cdb2:
        account = tdb.get(OutlookAccount, aid)
        if account is None:
            return {"messages_upserted": 0, "skipped": "no account"}

        folders_up = folders_del = msgs_up = msgs_rem = 0
        ingest_totals = new_totals()
        failed: list[dict] = []
        try:
            with with_outlook_client(cdb2, tdb, account.user_id, tid) as gc:
                folders_up, folders_del = _refresh_folder_cache(tdb, gc, account)
                tdb.commit()
                # Re-query under fresh state
                folders = (
                    tdb.query(OutlookFolder)
                    .filter(OutlookFolder.account_id == account.id)
                    .all()
                )
                # Vendor-bill auto-ingest allowlist (empty = feature off). Loaded
                # once per sync; candidates are COLLECTED during the folder loop
                # and ingested afterward in an isolated session (below), never
                # inside a folder-sync transaction.
                _settings = tdb.get(OutlookSettings, 1)
                allowlist = normalize_allowlist(
                    getattr(_settings, "vendor_bill_sender_allowlist", None) if _settings else None
                )
                vb_candidates: list[dict] = []
                for f in folders:
                    try:
                        u, r = _sync_one_folder(
                            tdb, gc, account, f,
                            allowlist=allowlist, candidates=vb_candidates,
                        )
                        msgs_up += u
                        msgs_rem += r
                        tdb.commit()
                    except OutlookGraphAPIError as exc:
                        log.warning("sync folder %s (%s) failed: %s",
                                    f.display_name, f.graph_folder_id, exc)
                        failed.append({"folder": f.display_name, "error": str(exc)[:200]})
                        tdb.rollback()

                # Every folder's sync state is now committed. Ingest the collected
                # vendor-bill candidates in an ISOLATED session (gc is still alive)
                # so the pipeline's flush/rollback/disk-I/O can't reach the sync
                # mirror or advance a delta token past un-mirrored messages.
                if allowlist and vb_candidates:
                    ingest_totals = _ingest_vendor_bills(
                        gc, vb_candidates, allowlist, stamp_account_id=account.id,
                    )
        except OutlookReconnectRequired as exc:
            log.warning("sync_outlook_mailbox: reconnect required for %s: %s", aid, exc)
            account.last_error = str(exc)[:500]
            tdb.commit()
            return {"error": str(exc)[:200]}

        account.last_sync_at = datetime.now(timezone.utc)
        account.last_error = None
        tdb.commit()
        return {
            "folders_upserted": folders_up,
            "folders_deleted": folders_del,
            "messages_upserted": msgs_up,
            "messages_removed": msgs_rem,
            "failed_folders": failed,
            "vendor_bills": ingest_totals,
        }


def _retag_untagged(tdb: Session, *, batch: int = 200, max_rows: int = 20000) -> dict:
    """Run tag_message over already-synced, still-untagged messages.

    D3 forward-tags NEW mail, but everything synced BEFORE tagging existed
    stays untagged and invisible to the by-customer/by-job tabs. This one-shot
    walks those rows (tag_strategy IS NULL, not deleted), tags what now
    matches, and commits in batches so a big mailbox doesn't hold one giant
    transaction. Idempotent: rows that don't match stay NULL and are simply
    re-tried on the next run — cheap (one indexed email_hash lookup + a regex).
    """
    scanned = tagged = 0
    last_id = None
    tag_settings = tdb.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    while scanned < max_rows:
        # OutlookMessage is hard-deleted (no deleted_at column) — the only
        # filter that matters is "not yet tagged".
        q = (
            tdb.query(OutlookMessage)
            .filter(OutlookMessage.tag_strategy.is_(None))
            .order_by(OutlookMessage.id)
        )
        if last_id is not None:
            q = q.filter(OutlookMessage.id > last_id)
        rows = q.limit(batch).all()
        if not rows:
            break
        for row in rows:
            last_id = row.id
            scanned += 1
            try:
                if tag_message(row, tdb, settings=tag_settings):
                    tagged += 1
            except Exception:  # noqa: BLE001
                log.exception("retag failed for id=%s (skipped)", row.id)
        tdb.commit()
    return {"scanned": scanned, "tagged": tagged}


@celery_app.task(name="outlook.retag_untagged_messages", bind=True)
def retag_untagged_messages(self, tenant_id: str | None = None, max_rows: int = 20000) -> dict:
    """One-shot re-tag of already-synced untagged mail (D3 backfill).

    Single-tenant: rows are scoped by the session, so tenant_id is accepted
    for call-site symmetry but not required to isolate.
    """
    with contextlib.closing(SessionLocal()) as tdb:
        return _retag_untagged(tdb, max_rows=max_rows)


@celery_app.task(name="outlook.backfill_outlook_mailbox", bind=True)
def backfill_outlook_mailbox(self, account_id: str, tenant_id: str, days: int = 90) -> dict:
    """Initial backfill on connect across all synced folders.

    For each folder (except SKIP_SYNC_WELL_KNOWN), pull the last `days`
    of messages, then prime that folder's delta token. Subsequent
    sync_outlook_mailbox runs will pick up only deltas.
    """
    aid = UUID(account_id)
    tid = UUID(tenant_id)
    with contextlib.closing(SessionLocal()) as tdb, \
         contextlib.closing(SessionLocal()) as cdb2:
        account = tdb.get(OutlookAccount, aid)
        if account is None:
            return {"messages_upserted": 0, "skipped": "no account"}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        total_upserted = 0
        folder_results: dict[str, int] = {}
        failed: list[dict] = []

        try:
            with with_outlook_client(cdb2, tdb, account.user_id, tid) as gc:
                # 1. Refresh folder cache so we know what to walk.
                folders_up, folders_del = _refresh_folder_cache(tdb, gc, account)
                tdb.commit()
                folders = (
                    tdb.query(OutlookFolder)
                    .filter(OutlookFolder.account_id == account.id)
                    .all()
                )

                # 2. Per-folder date-filtered pull.
                for f in folders:
                    if f.well_known_name and f.well_known_name in SKIP_SYNC_WELL_KNOWN:
                        continue
                    folder_total = 0
                    next_link: str | None = None
                    try:
                        while True:
                            if next_link:
                                resp = gc._request("GET", next_link).json()
                            else:
                                resp = gc._request(
                                    "GET",
                                    f"/me/mailFolders/{f.graph_folder_id}/messages",
                                    params={
                                        "$top": 100,
                                        "$orderby": "receivedDateTime desc",
                                        "$filter": f"receivedDateTime ge {cutoff}",
                                    },
                                ).json()
                            page_msgs = resp.get("value") or []
                            folder_total += _persist_messages(
                                tdb, account, page_msgs,
                                folder_id=f.graph_folder_id,
                                folder_display_name=f.display_name,
                                gc=gc,
                            )
                            next_link = resp.get("@odata.nextLink")
                            if not next_link:
                                break
                            if folder_total >= BACKFILL_MAX_MESSAGES_PER_RUN:
                                log.warning(
                                    "backfill cap hit (%d msgs) for folder %s — "
                                    "older mail beyond %d-day window may not be backfilled",
                                    BACKFILL_MAX_MESSAGES_PER_RUN, f.display_name, days,
                                )
                                break
                        # No delta-prime here: it called the PLAIN listing, which
                        # never returns a deltaLink, so it never primed anything —
                        # every folder's first real deltaLink comes from
                        # _sync_one_folder's bootstrap walk on the next sync.
                        folder_results[f.display_name] = folder_total
                        total_upserted += folder_total
                        tdb.commit()
                    except OutlookGraphAPIError as exc:
                        log.warning("backfill folder %s failed: %s", f.display_name, exc)
                        failed.append({"folder": f.display_name, "error": str(exc)[:200]})
                        tdb.rollback()
        except OutlookReconnectRequired as exc:
            log.warning("backfill: reconnect required for %s: %s", aid, exc)
            account.last_error = str(exc)[:500]
            tdb.commit()
            return {"messages_upserted": total_upserted, "error": str(exc)[:200]}

        account.last_sync_at = datetime.now(timezone.utc)
        account.last_error = None
        tdb.commit()
        return {
            "messages_upserted": total_upserted,
            "folders_upserted": folders_up,
            "folders_deleted": folders_del,
            "per_folder": folder_results,
            "failed_folders": failed,
        }


@celery_app.task(name="outlook.sweep_vendor_bill_history", bind=True)
def sweep_vendor_bill_history(self, account_id: str, tenant_id: str, days: int = 365) -> dict:
    """Repeatable vendor-bill history sweep (Phase 2, increment D3).

    Walks the LOCAL ``outlook_messages`` mirror — NOT a Graph listing — for
    un-checkpointed messages with attachments from allowlisted senders inside
    the ``days`` window, then downloads + pipelines their PDFs with a per-run
    download budget. History beyond the mirror is out of scope by design:
    extend the mirror first (bump backfill_days + re-run backfill), then sweep.

    Repeatable by construction: every cleanly-processed message is stamped
    (``vendor_bills_ingested_at``), so the next run only touches what previous
    runs didn't reach (budget-cut, errored, or newly mirrored). The report's
    ``cap_hit`` tells the admin another run is worth it. Empty allowlist =
    feature off = no-op.
    """
    aid = UUID(account_id)
    tid = UUID(tenant_id)
    with contextlib.closing(SessionLocal()) as tdb, \
         contextlib.closing(SessionLocal()) as cdb2:
        account = tdb.get(OutlookAccount, aid)
        if account is None:
            return {"skipped": "no account"}
        _settings = tdb.get(OutlookSettings, 1)
        allowlist = normalize_allowlist(
            getattr(_settings, "vendor_bill_sender_allowlist", None) if _settings else None
        )
        if not allowlist:
            return {"skipped": "allowlist empty"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # The sweep can only see what the mirror holds. Surface the mirror's
        # floor so "cap_hit: false" is never read as "the whole window is in"
        # when the mirror is shallower than the requested days (backfill_days
        # defaults to 90). window_covered=False => extend the mirror (bump
        # backfill_days + re-run backfill), then sweep again.
        mirror_oldest = (
            tdb.query(sa_func.min(OutlookMessage.received_at))
            .filter(OutlookMessage.account_id == account.id)
            .scalar()
        )
        if mirror_oldest is not None and mirror_oldest.tzinfo is None:
            mirror_oldest = mirror_oldest.replace(tzinfo=timezone.utc)
        window_covered = mirror_oldest is not None and mirror_oldest <= cutoff
        scanned = 0
        candidates: list[dict] = []
        q = (
            tdb.query(OutlookMessage)
            .filter(
                OutlookMessage.account_id == account.id,
                OutlookMessage.has_attachments.is_(True),
                OutlookMessage.vendor_bills_ingested_at.is_(None),
                OutlookMessage.from_address.isnot(None),
                OutlookMessage.received_at >= cutoff,
            )
            .order_by(OutlookMessage.received_at.desc())
        )
        # Sender allowlisting supports domain + subdomain matching, which SQL
        # can't express cleanly — scan the (index-served) candidate window and
        # filter in Python. The message cap bounds the batch, not coverage:
        # un-collected rows stay un-checkpointed for the next run.
        for row in q.yield_per(200):
            scanned += 1
            if not sender_allowed(row.from_address, allowlist):
                continue
            candidates.append({
                "id": row.graph_message_id,
                "hasAttachments": True,
                "from": {"emailAddress": {"address": row.from_address}},
            })
            if len(candidates) >= SWEEP_MAX_MESSAGES_PER_RUN:
                break

        totals = new_totals()
        totals["skipped_no_budget"] = 0
        totals["skipped_already_ingested"] = 0
        totals["quarantined"] = 0
        if candidates:
            try:
                with with_outlook_client(cdb2, tdb, account.user_id, tid) as gc:
                    totals = _ingest_vendor_bills(
                        gc, candidates, allowlist,
                        stamp_account_id=account.id,
                        max_downloads=SWEEP_MAX_DOWNLOADS_PER_RUN,
                    )
            except OutlookReconnectRequired as exc:
                log.warning("sweep_vendor_bill_history: reconnect required for %s: %s", aid, exc)
                account.last_error = str(exc)[:500]
                tdb.commit()
                return {"error": str(exc)[:200], "scanned": scanned,
                        "candidates": len(candidates), **totals}

        cap_hit = bool(
            totals["capped"]
            or totals.get("skipped_no_budget", 0)
            or len(candidates) >= SWEEP_MAX_MESSAGES_PER_RUN
        )
        result = {"scanned": scanned, "candidates": len(candidates),
                  "cap_hit": cap_hit, "days": days,
                  "mirror_oldest": mirror_oldest.isoformat() if mirror_oldest else None,
                  "window_covered": window_covered, **totals}
        log.info("sweep_vendor_bill_history %s: %s", aid, result)
        return result


@celery_app.task(name="outlook.repair_blank_outlook_messages", bind=True)
def repair_blank_outlook_messages(self, account_id: str, tenant_id: str, batch: int = 100) -> dict:
    """One-shot repair for rows blanked by the partial-delta overwrite bug.

    Rows where subject, from_address, AND body_preview are all NULL were
    populated once and then overwritten by partial delta items (bare-token
    resume, 2026-07). Each still has its graph_message_id: re-fetch the full
    message and re-persist the envelope. 404 → the message is gone from the
    mailbox → delete the row. Other Graph errors are counted and skipped so
    one bad message can't abort the pass. Idempotent — repaired rows no
    longer match the filter.
    """
    aid = UUID(account_id)
    tid = UUID(tenant_id)
    with contextlib.closing(SessionLocal()) as tdb, \
         contextlib.closing(SessionLocal()) as cdb2:
        account = tdb.get(OutlookAccount, aid)
        if account is None:
            return {"repaired": 0, "skipped": "no account"}

        repaired = deleted = errors = 0
        failed_ids: set = set()
        try:
            with with_outlook_client(cdb2, tdb, account.user_id, tid) as gc:
                while True:
                    q = (
                        tdb.query(OutlookMessage)
                        .filter(
                            OutlookMessage.account_id == account.id,
                            OutlookMessage.subject.is_(None),
                            OutlookMessage.from_address.is_(None),
                            OutlookMessage.body_preview.is_(None),
                        )
                        .order_by(OutlookMessage.id)
                    )
                    if failed_ids:
                        q = q.filter(OutlookMessage.id.notin_(failed_ids))
                    rows = q.limit(batch).all()
                    if not rows:
                        break
                    for row in rows:
                        try:
                            full = gc.get_message(row.graph_message_id)
                        except OutlookGraphAPIError as exc:
                            if exc.status_code == 404:
                                tdb.delete(row)
                                deleted += 1
                            else:
                                log.warning(
                                    "repair fetch failed for graph_id=%s: %s",
                                    row.graph_message_id, exc,
                                )
                                errors += 1
                                failed_ids.add(row.id)
                            continue
                        _persist_messages(tdb, account, [full])
                        if (row.subject is None and row.from_address is None
                                and row.body_preview is None):
                            # Graph really has no envelope for this message
                            # (e.g. an empty draft) — it would match the
                            # filter forever, so park it instead.
                            failed_ids.add(row.id)
                        else:
                            repaired += 1
                    tdb.commit()
        except OutlookReconnectRequired as exc:
            log.warning("repair: reconnect required for %s: %s", aid, exc)
            tdb.commit()
            return {"repaired": repaired, "deleted": deleted, "errors": errors,
                    "error": str(exc)[:200]}
        return {"repaired": repaired, "deleted": deleted, "errors": errors}


@celery_app.task(name="outlook.renew_all_outlook_subscriptions", bind=True)
def renew_all_outlook_subscriptions(self) -> dict:
    """Beat task: renew every OutlookSubscription nearing expiry, and
    CREATE one for any connected account that has none. Every 6 hours.

    The create half is the self-heal for the 2026-07-07 audit finding:
    nothing ever called create_subscription (the "on connect" docstring
    was aspirational) and a failed create was never retried, so prod ran
    with an empty outlook_subscriptions table and no real-time inbox."""
    from gdx_dispatch.modules.outlook.subscriptions import (
        RENEWAL_THRESHOLD_HOURS,
        SubscriptionError,
        create_subscription,
        renew_subscription,
    )

    tenant_id_str = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    renewed = failed = examined = created = create_failed = 0
    threshold = datetime.now(timezone.utc) + timedelta(hours=RENEWAL_THRESHOLD_HOURS)

    tdb = SessionLocal()
    try:
        expiring = (
            tdb.query(OutlookSubscription)
            .filter(OutlookSubscription.expiration_at <= threshold)
            .filter(OutlookSubscription.last_error.is_(None))
            .all()
        )
        for sub in expiring:
            examined += 1
            try:
                with contextlib.closing(SessionLocal()) as cdb2:
                    renew_subscription(
                        control_db=cdb2,
                        tenant_db=tdb,
                        tenant_id=UUID(tenant_id_str),
                        subscription=sub,
                    )
                tdb.commit()
                renewed += 1
            except SubscriptionError:
                log.exception(
                    "renew_all: sub=%s renewal failed (last_error persisted; will skip until cleared)",
                    sub.id,
                )
                failed += 1

        # Self-heal: create a subscription for any connected account
        # (tokens present) that has no subscription row at all.
        subless = (
            tdb.query(OutlookAccount)
            .outerjoin(OutlookSubscription, OutlookSubscription.account_id == OutlookAccount.id)
            .filter(OutlookAccount.access_token_enc.isnot(None))
            .filter(OutlookSubscription.id.is_(None))
            .all()
        )
        for account in subless:
            try:
                with contextlib.closing(SessionLocal()) as cdb2:
                    create_subscription(
                        control_db=cdb2,
                        tenant_db=tdb,
                        tenant_id=UUID(tenant_id_str),
                        user_id=UUID(str(account.user_id)),
                    )
                tdb.commit()
                created += 1
                log.info("renew_all: created missing subscription for account=%s", account.id)
            except SubscriptionError:
                tdb.rollback()
                log.exception("renew_all: create failed for account=%s (fallback poll still covers sync)", account.id)
                create_failed += 1
    except Exception:
        log.warning("renew_all: tenant %s skipped (likely missing outlook tables)", tenant_id_str)
    finally:
        tdb.close()
    return {
        "examined": examined,
        "renewed": renewed,
        "failed": failed,
        "created": created,
        "create_failed": create_failed,
    }


@celery_app.task(name="outlook.poll_outlook_mailboxes_fallback", bind=True)
def poll_outlook_mailboxes_fallback(self) -> dict:
    """Beat task: trigger sync for any connected OutlookAccount whose
    subscription is missing/expired/errored. Webhook safety net. 15-min cadence."""
    tenant_id_str = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    triggered = skipped_healthy = skipped_disconnected = 0
    now = datetime.now(timezone.utc)

    tdb = SessionLocal()
    try:
        accounts = (
            tdb.query(OutlookAccount)
            .filter(OutlookAccount.access_token_enc.isnot(None))
            .all()
        )
        for account in accounts:
            sub = (
                tdb.query(OutlookSubscription)
                .filter(OutlookSubscription.account_id == account.id)
                .one_or_none()
            )
            healthy = (
                sub is not None
                and sub.expiration_at > now
                and not sub.last_error
            )
            if healthy:
                skipped_healthy += 1
                continue
            if account.access_token_enc is None:
                skipped_disconnected += 1
                continue
            sync_outlook_mailbox.delay(str(account.id), tenant_id_str)
            triggered += 1
    except Exception:
        # DB may lack outlook_* tables (module not provisioned).
        log.warning("fallback_poll: tenant %s skipped (likely missing outlook tables)", tenant_id_str)
    finally:
        tdb.close()
    return {
        "triggered": triggered,
        "skipped_healthy": skipped_healthy,
        "skipped_disconnected": skipped_disconnected,
    }
