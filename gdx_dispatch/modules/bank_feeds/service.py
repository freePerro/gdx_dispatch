"""Bank Feeds sync service — accounts, transactions, statement documents.

Watermark strategy (audited plan rev 3):

- BACKFILL walks 90-day ``since``/``until`` windows (1-day overlap), oldest
  first, committing ``backfill_synced_through`` after each window so a run
  that dies resumes instead of redoing history. On completion the
  incremental cursor anchors at ``backfill_started_at − 5 min`` — anything
  mutated DURING the backfill has ``lastUpdated`` ≥ that anchor.
- INCREMENTAL queries ``updatedSince=cursor`` and advances the cursor ONLY
  when rows were received, to ``max(old, max(lastUpdated) − 5 min)`` —
  Banno's timebase, never ours. Advancing on an empty run would mix our
  clock into their timebase and can permanently skip late-stamped rows.
- Documents sweep is metadata-first: listing inserts rows with
  ``fetched_at = NULL``; downloads fill them in. The per-connection
  ``documents_synced_through`` cursor advances only when NO row in the
  sweep window is left unfetched — NULL ``fetched_at`` rows are the retry
  queue, so partial download failures can never become silent gaps.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.bank_feeds.client import (
    BannoClient,
    BannoDocumentsUnavailable,
)
from gdx_dispatch.modules.bank_feeds.models import (
    FREQ_DAILY,
    FREQ_EVERY_4H,
    FREQ_HOURLY,
    FREQ_MANUAL,
    FREQ_WEEKLY,
    VALID_FREQUENCIES,
    BankFeedAccount,
    BankFeedDocument,
    BankFeedSyncSchedule,
    BankFeedTransaction,
    BannoConnection,
)

log = logging.getLogger(__name__)

BACKFILL_WINDOW_DAYS = 90
BACKFILL_WINDOW_OVERLAP_DAYS = 1
CURSOR_OVERLAP = timedelta(minutes=5)
DOCUMENTS_RELIST_OVERLAP_DAYS = 35


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for DateTime(timezone=True) columns;
    normalize to UTC-aware before any comparison."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── tenant timezone ────────────────────────────────────────────────────


def tenant_zoneinfo(db: Session) -> ZoneInfo:
    """Tenant timezone from AppSettings (single source of truth — the
    audited plan explicitly rejects hardcoding a zone here)."""
    tz_name = "America/New_York"
    try:
        from gdx_dispatch.models.tenant_models import AppSettings  # noqa: PLC0415

        row = db.execute(select(AppSettings.timezone)).first()
        if row and row[0]:
            tz_name = str(row[0])
    except Exception:  # noqa: BLE001
        pass
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("America/New_York")


# ── parsing helpers ────────────────────────────────────────────────────

_TWO_DP = Decimal("0.01")


def parse_amount_cents(raw: Any) -> int | None:
    """Signed cents from Banno's string amount.

    Exact-2dp values convert exactly. Sub-cent values (rare interest/fee
    postings on some cores) quantize ROUND_HALF_EVEN with a warning.
    Missing/unparseable → None (row is stored; consumers filter NOT NULL).
    """
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        log.warning("bank_feeds_amount_unparseable raw=%r", str(raw)[:40])
        return None
    if not value.is_finite():
        log.warning("bank_feeds_amount_nonfinite raw=%r", str(raw)[:40])
        return None
    quantized = value.quantize(_TWO_DP, rounding=ROUND_HALF_EVEN)
    if quantized != value:
        log.warning("bank_feeds_amount_subcent raw=%r stored=%s", str(raw)[:40], quantized)
    return int(quantized * 100)


def parse_instant(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_description(txn: dict) -> str:
    text = str(txn.get("filteredMemo") or txn.get("memo") or txn.get("displayName") or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def compute_line_hash(
    external_account_id: str, posted_date: date | None, amount_cents: int | None, txn: dict
) -> str | None:
    """GL-Phase-2-compatible content fingerprint. POSTED rows only — the
    inputs aren't stable while pending."""
    if posted_date is None or amount_cents is None:
        return None
    basis = f"{external_account_id}|{posted_date.isoformat()}|{amount_cents}|{_normalize_description(txn)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ── accounts sync ──────────────────────────────────────────────────────


def sync_accounts(db: Session, client: BannoClient, connection: BannoConnection) -> dict:
    payload = client.get_accounts(connection.banno_user_id)
    accounts = payload.get("accounts") or []
    inactivated = {str(a) for a in (payload.get("inactivatedAccountIds") or [])}
    now = _utcnow()
    created = updated = 0

    existing = {
        a.external_account_id: a
        for a in db.execute(
            select(BankFeedAccount).where(BankFeedAccount.connection_id == connection.id)
        ).scalars()
    }

    for acct in accounts:
        ext_id = str(acct.get("id") or "")
        if not ext_id:
            continue
        row = existing.get(ext_id)
        if row is None:
            row = BankFeedAccount(connection_id=connection.id, external_account_id=ext_id)
            db.add(row)
            existing[ext_id] = row
            created += 1
        else:
            updated += 1
        row.name = str(acct.get("name") or acct.get("nickname") or "") or row.name
        row.account_type = str(acct.get("accountType") or acct.get("type") or "") or row.account_type
        row.account_subtype = (
            str(acct.get("accountSubType") or acct.get("subType") or "") or row.account_subtype
        )
        masked = str(acct.get("maskedNumber") or acct.get("accountNumberMasked") or "")
        if not masked:
            number_tail = str(acct.get("lastFour") or "")
            masked = f"•{number_tail}" if number_tail else ""
        if masked:
            row.account_number_masked = masked[-30:]
        for source_key, attr in (("balance", "balance"), ("availableBalance", "available_balance")):
            parsed = parse_amount_cents(acct.get(source_key))
            if parsed is not None:
                setattr(row, attr, Decimal(parsed) / 100)
        fetched = parse_instant(acct.get("fetchedDate"))
        if fetched:
            row.balance_as_of = fetched
        row.is_inactive = ext_id in inactivated
        row.raw_json = acct
        row.updated_at = now

    for ext_id in inactivated:
        row = existing.get(ext_id)
        if row is not None and not row.is_inactive:
            row.is_inactive = True
            row.updated_at = now

    db.commit()
    return {"created": created, "updated": updated, "inactivated": len(inactivated)}


# ── transactions sync ──────────────────────────────────────────────────


def _upsert_transaction(
    db: Session,
    account: BankFeedAccount,
    txn: dict,
    tz: ZoneInfo,
    now: datetime,
    existing_by_ext: dict[str, BankFeedTransaction],
) -> datetime | None:
    ext_id = str(txn.get("id") or "")
    if not ext_id:
        return None
    row = existing_by_ext.get(ext_id)
    if row is None:
        row = db.execute(
            select(BankFeedTransaction).where(
                BankFeedTransaction.account_id == account.id,
                BankFeedTransaction.external_transaction_id == ext_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = BankFeedTransaction(account_id=account.id, external_transaction_id=ext_id)
            db.add(row)
        existing_by_ext[ext_id] = row

    amount_cents = parse_amount_cents(txn.get("amount"))
    if amount_cents is not None or row.amount_cents is None:
        row.amount_cents = amount_cents

    pending_status = str(txn.get("pendingStatus") or "").strip().lower()
    row.pending = pending_status not in ("", "posted", "reconciled", "complete", "completed")

    posted_at = parse_instant(txn.get("datePosted"))
    if posted_at:
        row.posted_at = posted_at
        row.posted_date = posted_at.astimezone(tz).date()

    row.payee = str(txn.get("displayName") or "")[:300] or row.payee
    row.memo = txn.get("memo") or row.memo
    row.filtered_memo = txn.get("filteredMemo") or row.filtered_memo
    row.check_number = (str(txn.get("checkNumber") or "")[:20] or None) or row.check_number
    running = parse_amount_cents(txn.get("runningBalance"))
    if running is not None:  # display-only; never overwrite non-null with null
        row.running_balance_cents = running
    row.txn_type = str(txn.get("type") or "")[:50] or row.txn_type
    row.txn_subtype = str(txn.get("subtype") or "")[:50] or row.txn_subtype
    merchant = txn.get("merchant") or {}
    if isinstance(merchant, dict) and merchant.get("name"):
        row.merchant_name = str(merchant["name"])[:300]
    enrichments = txn.get("enrichments") or {}
    if isinstance(enrichments, dict) and enrichments.get("expenseCategory"):
        row.category = str(enrichments["expenseCategory"])[:120]

    row.line_hash = (
        None if row.pending
        else compute_line_hash(account.external_account_id, row.posted_date, row.amount_cents, txn)
    )
    row.external_last_updated = parse_instant(txn.get("lastUpdated"))
    if row.deleted_at is not None:
        row.deleted_at = None  # reappearance clears the tombstone
    row.raw_json = txn
    row.last_synced_at = now
    row.updated_at = now
    return row.external_last_updated


def _apply_inactivations(
    db: Session, account: BankFeedAccount, inactivated_ids: set[str], now: datetime
) -> int:
    """Tombstone listed ids. Ids never synced are a no-op — no phantom rows."""
    if not inactivated_ids:
        return 0
    rows = db.execute(
        select(BankFeedTransaction).where(
            BankFeedTransaction.account_id == account.id,
            BankFeedTransaction.external_transaction_id.in_(sorted(inactivated_ids)),
            BankFeedTransaction.deleted_at.is_(None),
        )
    ).scalars().all()
    for row in rows:
        row.deleted_at = now
        row.updated_at = now
    return len(rows)


def _iso_instant(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_account_transactions(
    db: Session,
    client: BannoClient,
    connection: BannoConnection,
    account: BankFeedAccount,
    *,
    backfill_days: int,
) -> dict:
    """Backfill (resumable windows) or incremental (updatedSince)."""
    tz = tenant_zoneinfo(db)
    now = _utcnow()
    stats = {"upserted": 0, "tombstoned": 0, "mode": ""}
    existing_by_ext: dict[str, BankFeedTransaction] = {}

    if account.full_resync_required:
        account.initial_backfill_done = False
        account.backfill_started_at = None
        account.backfill_synced_through = None
        account.updated_since_cursor = None
        account.full_resync_required = False
        db.commit()

    if not account.initial_backfill_done:
        stats["mode"] = "backfill"
        if account.backfill_started_at is None:
            account.backfill_started_at = now
            db.commit()
        horizon_start = (now - timedelta(days=backfill_days)).date()
        resume_from = account.backfill_synced_through or horizon_start
        window_start = max(
            horizon_start,
            resume_from - timedelta(days=BACKFILL_WINDOW_OVERLAP_DAYS),
        )
        end_date = now.date()

        while window_start <= end_date:
            window_end = min(window_start + timedelta(days=BACKFILL_WINDOW_DAYS), end_date)
            since = _iso_instant(datetime.combine(window_start, datetime.min.time(), tzinfo=timezone.utc))
            until = _iso_instant(
                datetime.combine(window_end, datetime.max.time().replace(microsecond=0), tzinfo=timezone.utc)
            )
            for page in client.iter_transaction_pages(
                connection.banno_user_id, account.external_account_id, since=since, until=until
            ):
                for txn in page.get("transactions") or []:
                    _upsert_transaction(db, account, txn, tz, now, existing_by_ext)
                    stats["upserted"] += 1
                stats["tombstoned"] += _apply_inactivations(
                    db, account,
                    {str(i) for i in (page.get("inactivatedTransactionIds") or [])},
                    now,
                )
            # Window complete — commit progress so a dead run RESUMES here.
            account.backfill_synced_through = window_end
            account.updated_at = now
            db.commit()
            if window_end >= end_date:
                break
            window_start = window_end - timedelta(days=BACKFILL_WINDOW_OVERLAP_DAYS)

        account.initial_backfill_done = True
        account.updated_since_cursor = (
            _ensure_aware(account.backfill_started_at) - CURSOR_OVERLAP
        )
        account.last_synced_at = now
        db.commit()
        return stats

    # Incremental.
    stats["mode"] = "incremental"
    cursor = _ensure_aware(account.updated_since_cursor) or (now - timedelta(days=backfill_days))
    max_last_updated: datetime | None = None
    received = 0
    for page in client.iter_transaction_pages(
        connection.banno_user_id, account.external_account_id,
        updated_since=_iso_instant(cursor),
    ):
        for txn in page.get("transactions") or []:
            received += 1
            stats["upserted"] += 1
            lu = _upsert_transaction(db, account, txn, tz, now, existing_by_ext)
            if lu and (max_last_updated is None or lu > max_last_updated):
                max_last_updated = lu
        stats["tombstoned"] += _apply_inactivations(
            db, account,
            {str(i) for i in (page.get("inactivatedTransactionIds") or [])},
            now,
        )

    # Cursor advances ONLY on received rows, in Banno's timebase.
    if received and max_last_updated is not None:
        candidate = max_last_updated - CURSOR_OVERLAP
        if candidate > cursor:
            account.updated_since_cursor = candidate
    account.last_synced_at = now
    db.commit()
    return stats


# ── documents (statement archive) ──────────────────────────────────────


def _documents_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads/")) / "bank_statements"


def probe_documents(client: BannoClient, connection: BannoConnection, db: Session) -> dict | None:
    """Eligibility probe. Returns institution documents settings or None.

    ``documents_available=False`` is NOT sticky — the caller re-probes on
    manual sync / reconnect (audited plan S10); only True short-circuits.
    """
    try:
        settings = client.get_documents_institution_settings(connection.banno_user_id)
    except BannoDocumentsUnavailable:
        connection.documents_available = False
        db.commit()
        return None
    doc_types = settings.get("documentTypes") or []
    connection.documents_available = bool(doc_types)
    db.commit()
    return settings if doc_types else None


def sync_documents(
    db: Session,
    client: BannoClient,
    connection: BannoConnection,
    *,
    backfill_days: int,
    force_probe: bool = False,
) -> dict:
    stats = {"listed": 0, "downloaded": 0, "failed": 0, "skipped": False}

    if connection.documents_available is False and not force_probe:
        stats["skipped"] = True
        return stats
    settings = probe_documents(client, connection, db)
    if settings is None:
        stats["skipped"] = True
        return stats

    today = _utcnow().date()
    floor = today - timedelta(days=backfill_days)
    doc_start = None
    try:
        raw_start = settings.get("documentStartDate")
        if raw_start:
            doc_start = date.fromisoformat(str(raw_start)[:10])
    except ValueError:
        doc_start = None
    if doc_start and doc_start > floor:
        floor = doc_start
    if connection.documents_synced_through:
        resume = connection.documents_synced_through - timedelta(days=DOCUMENTS_RELIST_OVERLAP_DAYS)
        if resume > floor:
            floor = resume

    docs = client.list_documents(
        connection.banno_user_id,
        start_date=floor.isoformat(),
        end_date=today.isoformat(),
    )
    now = _utcnow()

    existing = {
        d.external_document_id: d
        for d in db.execute(
            select(BankFeedDocument).where(BankFeedDocument.connection_id == connection.id)
        ).scalars()
    }

    # Metadata first — rows with fetched_at NULL are the retry queue.
    for doc in docs:
        ext_id = str(doc.get("documentId") or "")
        if not ext_id:
            continue
        stats["listed"] += 1
        row = existing.get(ext_id)
        if row is None:
            row = BankFeedDocument(connection_id=connection.id, external_document_id=ext_id)
            db.add(row)
            existing[ext_id] = row
        row.document_type = str(doc.get("documentType") or "statement")[:20]
        row.title = str(doc.get("documentTitle") or "")[:300] or row.title
        row.filename = str(doc.get("documentFilename") or "")[:200] or row.filename
        try:
            row.document_date = date.fromisoformat(str(doc.get("date") or "")[:10])
        except ValueError:
            pass
        raw_accounts = doc.get("accountIds")
        row.account_ids = [str(a) for a in raw_accounts] if isinstance(raw_accounts, list) else []
        row.updated_at = now
    db.commit()

    # Download every unfetched row (this sweep's + any prior failures).
    unfetched = db.execute(
        select(BankFeedDocument).where(
            BankFeedDocument.connection_id == connection.id,
            BankFeedDocument.fetched_at.is_(None),
        )
    ).scalars().all()
    directory = _documents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for row in unfetched:
        try:
            content, content_type = client.download_document(
                connection.banno_user_id, row.external_document_id
            )
        except Exception:  # noqa: BLE001 — recorded; row stays queued
            log.warning(
                "bank_feeds_document_download_failed doc=%s", row.external_document_id
            )
            stats["failed"] += 1
            continue
        storage_name = f"{_uuid.uuid4()}.pdf"
        path = directory / storage_name
        path.write_bytes(content)
        row.storage_path = str(path)
        row.sha256 = hashlib.sha256(content).hexdigest()
        row.size_bytes = len(content)
        row.content_type = content_type[:100]
        row.fetched_at = _utcnow()
        row.updated_at = row.fetched_at
        stats["downloaded"] += 1
        db.commit()

    # Cursor advances ONLY when nothing is left unfetched (audited plan B3).
    if stats["failed"] == 0:
        remaining = db.execute(
            select(BankFeedDocument.id).where(
                BankFeedDocument.connection_id == connection.id,
                BankFeedDocument.fetched_at.is_(None),
            )
        ).first()
        if remaining is None:
            connection.documents_synced_through = today
            db.commit()
    return stats


# ── schedule helpers (QBSyncSchedule pattern, module-local copy) ───────


def _frequency_delta(frequency: str) -> timedelta | None:
    return {
        FREQ_HOURLY: timedelta(hours=1),
        FREQ_EVERY_4H: timedelta(hours=4),
        FREQ_DAILY: timedelta(days=1),
        FREQ_WEEKLY: timedelta(days=7),
    }.get(frequency)


def compute_next_run_at(frequency: str, base: datetime | None = None) -> datetime | None:
    delta = _frequency_delta(frequency)
    if delta is None:
        return None
    return (base or _utcnow()) + delta


def get_or_create_schedule(db: Session) -> BankFeedSyncSchedule:
    row = db.execute(
        select(BankFeedSyncSchedule).order_by(BankFeedSyncSchedule.created_at.asc())
    ).scalars().first()
    if row is None:
        row = BankFeedSyncSchedule(frequency=FREQ_MANUAL)
        db.add(row)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 — concurrent insert race
            db.rollback()
            row = db.execute(
                select(BankFeedSyncSchedule).order_by(BankFeedSyncSchedule.created_at.asc())
            ).scalars().first()
            if row is None:
                raise
        db.refresh(row)
    return row


def update_schedule(
    db: Session, frequency: str, *, backfill_days: int | None = None
) -> BankFeedSyncSchedule:
    if frequency not in VALID_FREQUENCIES:
        raise ValueError(f"invalid frequency: {frequency}")
    row = get_or_create_schedule(db)
    row.frequency = frequency
    row.next_run_at = compute_next_run_at(frequency)
    if backfill_days is not None:
        row.backfill_days = max(1, min(int(backfill_days), 3650))
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


def schedule_dict(s: BankFeedSyncSchedule) -> dict[str, Any]:
    return {
        "frequency": s.frequency,
        "backfill_days": s.backfill_days,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "last_run_status": s.last_run_status,
        "last_run_error": s.last_run_error,
    }


def record_scheduled_run(db: Session, status: str, error: str | None = None) -> None:
    s = get_or_create_schedule(db)
    now = _utcnow()
    s.last_run_at = now
    s.last_run_status = status
    s.last_run_error = (error or "")[:500] if error else None
    s.next_run_at = compute_next_run_at(s.frequency, base=now)
    db.commit()
