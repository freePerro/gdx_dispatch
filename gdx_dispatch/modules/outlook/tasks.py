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
from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired, with_outlook_client
from gdx_dispatch.modules.outlook.vendor_bill_ingest import (
    ingest_message_attachments,
    is_candidate,
    normalize_allowlist,
)

log = logging.getLogger("gdx_dispatch.modules.outlook.tasks")

MAX_MESSAGES_PER_RUN = 500
BACKFILL_MAX_MESSAGES_PER_RUN = 5000

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


def _persist_messages(
    tdb: Session,
    account: OutlookAccount,
    graph_messages: list[dict[str, Any]],
    *,
    folder_id: str | None = None,
    folder_display_name: str | None = None,
) -> int:
    """Upsert OutlookMessage rows from a Graph response. Returns count touched.

    folder_id / folder_display_name are stamped on each row so the SPA can
    filter by folder. Pass None when the call site is folder-agnostic
    (legacy callers pre-folder-sync); rows from such calls inherit whatever
    folder_id the upsert path defaulted to.
    """
    upserted = 0
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
        if existing is None:
            row = OutlookMessage()
            row.account_id = account.id
            row.graph_message_id = graph_id
            tdb.add(row)
        else:
            row = existing
        row.internet_message_id = m.get("internetMessageId")
        row.conversation_id = m.get("conversationId")
        row.subject = m.get("subject")
        from_field = (m.get("from") or {}).get("emailAddress") or {}
        row.from_address = from_field.get("address")
        row.to_addresses = [
            r.get("emailAddress", {}).get("address")
            for r in (m.get("toRecipients") or [])
        ]
        row.cc_addresses = [
            r.get("emailAddress", {}).get("address")
            for r in (m.get("ccRecipients") or [])
        ]
        row.bcc_addresses = [
            r.get("emailAddress", {}).get("address")
            for r in (m.get("bccRecipients") or [])
        ]
        row.body_preview = m.get("bodyPreview")
        row.has_attachments = bool(m.get("hasAttachments"))
        row.is_read = bool(m.get("isRead"))
        # in_reply_to: Graph doesn't expose this as a selectable property;
        # threading is via conversation_id. Leave NULL until/unless the
        # singleValueExtendedProperties expansion is wired.
        row.received_at = _parse_iso(m.get("receivedDateTime"))
        row.sent_at = _parse_iso(m.get("sentDateTime"))
        # direction: outbound when sender == mailbox owner; inbound otherwise.
        # Default in model is "inbound", but explicit derivation prevents
        # sent-folder syncs from being mislabeled as inbound.
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

    delta_token = None if state.full_resync_required else state.delta_token
    upserted = removed = 0
    next_link: str | None = None
    last_resp: dict[str, Any] = {}
    try:
        while True:
            if next_link:
                last_resp = gc._request("GET", next_link).json()
            else:
                # list_messages_delta, not list_messages: the plain listing
                # never returns a deltaLink, so a token could never
                # bootstrap and every sync walked the whole mailbox.
                last_resp = gc.list_messages_delta(
                    folder=folder.graph_folder_id,
                    delta_token=delta_token,
                    top=100,
                )
            page_msgs = last_resp.get("value") or []
            removed += _apply_message_deletes(tdb, account, page_msgs)
            non_removed = [m for m in page_msgs if not m.get("@removed")]
            upserted += _persist_messages(
                tdb, account, non_removed,
                folder_id=folder.graph_folder_id,
                folder_display_name=folder.display_name,
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
            tok = _extract_delta_token(delta_link)
            if tok:
                state.delta_token = tok
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


def _ingest_vendor_bills(gc, candidates: list[dict], allowlist: list[str]) -> dict[str, int]:
    """Download + pipeline the collected vendor-bill candidates in a session
    fully isolated from the sync's tenant session. Each message commits on its
    own, and a poisoned session is discarded + replaced — so the pipeline's
    flush/rollback/disk-I/O can never reach the sync mirror, and one bad bill
    can't abort the whole sync."""
    totals = {"ingested": 0, "duplicate": 0, "unparseable": 0, "errors": 0}
    if not candidates or not allowlist:
        return totals
    idb = SessionLocal()
    try:
        for m in candidates:
            try:
                r = ingest_message_attachments(idb, gc, m, allowlist)
                idb.commit()
                for k, v in r.items():
                    totals[k] += v
            except Exception:  # noqa: BLE001
                log.exception("vendor_bill_ingest: message %s failed", m.get("id"))
                totals["errors"] += 1
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


def _extract_delta_token(delta_link: str | None) -> str | None:
    if not delta_link:
        return None
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(delta_link).query).get("$deltatoken", [None])[0]


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
        ingest_totals = {"ingested": 0, "duplicate": 0, "unparseable": 0, "errors": 0}
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
                    ingest_totals = _ingest_vendor_bills(gc, vb_candidates, allowlist)
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
                        # Prime per-folder delta token.
                        try:
                            delta_resp = gc.list_messages(
                                folder=f.graph_folder_id, delta_token=None, top=1,
                            )
                            tok = _extract_delta_token(delta_resp.get("@odata.deltaLink"))
                            if tok:
                                state = (
                                    tdb.query(OutlookFolderSyncState)
                                    .filter(
                                        OutlookFolderSyncState.account_id == account.id,
                                        OutlookFolderSyncState.folder_id == f.graph_folder_id,
                                    )
                                    .one_or_none()
                                )
                                if state is None:
                                    state = OutlookFolderSyncState()
                                    state.account_id = account.id
                                    state.folder_id = f.graph_folder_id
                                    tdb.add(state)
                                state.delta_token = tok
                                state.last_sync_at = datetime.now(timezone.utc)
                                state.last_error = None
                                state.full_resync_required = False
                        except OutlookGraphAPIError as exc:
                            log.warning("delta-prime failed for folder %s: %s", f.display_name, exc)
                            # Backfill data is in; delta will fall back to full resync next cycle.
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
