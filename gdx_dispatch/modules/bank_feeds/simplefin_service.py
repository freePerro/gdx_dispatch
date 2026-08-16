"""SimpleFIN sync service — normalization, windows, watermarks, ledger.

Differences from the Banno path that shape this module:

- ONE ``GET /accounts`` returns every account and its transactions, so sync
  is per-CONNECTION, not per-account. Backfill walks 90-day windows (the
  bridge's max) counting one fetch each; incremental is a single fetch
  overlapping the last synced date by ``FETCH_OVERLAP_DAYS`` (bridge
  guidance: ~5 days so nothing is missed).
- There is no ``updatedSince`` mutation feed. The per-account watermark is
  ``backfill_synced_through`` (a posted-date horizon). ``act.failed`` /
  ``act.missingdata`` errors freeze THAT account's watermark — advancing on
  incomplete listings would skip transactions forever. The monthly
  statement tie-out is the backstop for bank-side edits beyond the overlap.
- POSTED-ONLY ingest: pending transactions are never requested and are
  dropped defensively if present — a pending txn can post under a
  different id, which would leave a phantom duplicate.
- Fetch ledger: scheduled AND manual fetches share one counter on the
  schedule singleton, per TENANT-LOCAL calendar day, capped at
  ``daily_fetch_cap`` (≤ ``DAILY_FETCH_CAP_MAX`` = 20; the bridge allows 24
  and disables tokens that persistently exceed it).
- Balance snapshots: one row per account per local day (last observation
  wins) — the cash-curve history the account row's overwritten balance
  can't provide.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.bank_feeds import service
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    AUTH_NEEDS_RECONNECT,
    DAILY_FETCH_CAP_MAX,
    PROVIDER_SIMPLEFIN,
    BankFeedAccount,
    BankFeedBalanceSnapshot,
    BankFeedSyncSchedule,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.bank_feeds.simplefin_client import (
    SimpleFINAuthError,
    SimpleFINClient,
    SimpleFINError,
)

log = logging.getLogger(__name__)

FETCH_OVERLAP_DAYS = 5
# The bridge limits a request's date range to "90 days at a time" without
# saying whether the bound is inclusive. 88 (+1 exclusive end-day = an
# 89-day epoch span) is safely inside EITHER reading; the cost is one extra
# fetch per ~year of backfill. Do not "optimize" this back to 89/90 without
# proving a full-width window against the real bridge.
WINDOW_DAYS = 88
STALE_AFTER_HOURS = 48

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TRAILING_DIGITS_RE = re.compile(r"(\d{3,4})\s*$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── fetch-hours window (tenant-local, Doug 2026-08-13) ─────────────────


def parse_hhmm(value: str | None) -> dt_time | None:
    if not value:
        return None
    m = _HHMM_RE.match(value.strip())
    if not m:
        return None
    return dt_time(int(m.group(1)), int(m.group(2)))


def within_fetch_window(schedule: BankFeedSyncSchedule, now_local: dt_time) -> bool:
    """True when scheduled fetching is allowed at this LOCAL wall-clock time.

    No window (either bound missing/invalid) = always allowed. start == end
    is degenerate and treated as no window. start > end wraps midnight
    (e.g. 22:00–06:00). The comparison happens per-dispatch on the wall
    clock, so DST needs no special handling.
    """
    start = parse_hhmm(getattr(schedule, "fetch_window_start", None))
    end = parse_hhmm(getattr(schedule, "fetch_window_end", None))
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= now_local < end
    return now_local >= start or now_local < end


# ── fetch ledger (shared by scheduled AND manual) ──────────────────────


def _roll_ledger(db: Session, schedule: BankFeedSyncSchedule, today_local: date) -> None:
    if schedule.fetch_count_date != today_local:
        schedule.fetch_count_date = today_local
        schedule.fetch_count_today = 0
        db.commit()


def effective_cap(schedule: BankFeedSyncSchedule) -> int:
    cap = getattr(schedule, "daily_fetch_cap", DAILY_FETCH_CAP_MAX) or DAILY_FETCH_CAP_MAX
    return max(1, min(int(cap), DAILY_FETCH_CAP_MAX))


def quota_state(db: Session, schedule: BankFeedSyncSchedule, tz: ZoneInfo) -> dict:
    today_local = _utcnow().astimezone(tz).date()
    _roll_ledger(db, schedule, today_local)
    cap = effective_cap(schedule)
    used = int(schedule.fetch_count_today or 0)
    return {"used": used, "cap": cap, "remaining": max(0, cap - used), "date": today_local.isoformat()}


def record_fetches(db: Session, schedule: BankFeedSyncSchedule, n: int, tz: ZoneInfo) -> None:
    if n <= 0:
        return
    today_local = _utcnow().astimezone(tz).date()
    _roll_ledger(db, schedule, today_local)
    schedule.fetch_count_today = int(schedule.fetch_count_today or 0) + n
    schedule.updated_at = _utcnow()
    db.commit()


# ── connection helpers ─────────────────────────────────────────────────


def get_simplefin_institution(db: Session) -> BannoInstitution | None:
    return db.execute(
        select(BannoInstitution).where(BannoInstitution.provider == PROVIDER_SIMPLEFIN)
    ).scalars().first()


def get_simplefin_connection(db: Session, institution: BannoInstitution) -> BannoConnection | None:
    return db.execute(
        select(BannoConnection).where(BannoConnection.institution_id == institution.id)
    ).scalars().first()


def build_client(connection: BannoConnection) -> SimpleFINClient:
    from gdx_dispatch.modules.bank_feeds import oauth  # noqa: PLC0415

    base_url = (connection.provider_base_url or "").strip()
    secret = oauth._decrypt(connection.access_token_enc or "")
    if not base_url or ":" not in secret:
        raise SimpleFINAuthError("SimpleFIN connection has no stored credentials")
    username, _, password = secret.partition(":")
    return SimpleFINClient(base_url, username, password)


# ── normalization ──────────────────────────────────────────────────────


def _epoch_to_dt(raw: Any) -> datetime | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _mask_from_name(name: str) -> str | None:
    """Best-effort last-digits mask (MX names often end in the last 4).
    Feeds the statement tie-out's account pairing suggestion only."""
    m = _TRAILING_DIGITS_RE.search(name or "")
    return f"•{m.group(1)}" if m else None


def _upsert_sfin_account(
    db: Session, connection: BannoConnection, acct: dict, tz: ZoneInfo, now: datetime,
    existing: dict[str, BankFeedAccount],
) -> BankFeedAccount | None:
    ext_id = str(acct.get("id") or "")
    if not ext_id:
        return None
    row = existing.get(ext_id)
    if row is None:
        row = BankFeedAccount(
            connection_id=connection.id,
            external_account_id=ext_id,
            provider=PROVIDER_SIMPLEFIN,
        )
        db.add(row)
        existing[ext_id] = row
    row.provider = PROVIDER_SIMPLEFIN
    name = str(acct.get("name") or "")
    if name:
        row.name = name[:300]
        masked = _mask_from_name(name)
        if masked and not row.account_number_masked:
            row.account_number_masked = masked
    currency = str(acct.get("currency") or "")
    # Custom currencies are URLs per spec — not money we track; keep USD.
    if len(currency) == 3:
        row.currency = currency.upper()
    for source_key, attr in (("balance", "balance"), ("available-balance", "available_balance")):
        cents = service.parse_amount_cents(acct.get(source_key))
        if cents is not None:
            setattr(row, attr, Decimal(cents) / 100)
    as_of = _epoch_to_dt(acct.get("balance-date"))
    if as_of:
        row.balance_as_of = as_of
    raw = {k: v for k, v in acct.items() if k != "transactions"}
    row.raw_json = raw
    row.updated_at = now
    return row


def _upsert_balance_snapshot(
    db: Session, account: BankFeedAccount, acct: dict, tz: ZoneInfo, now: datetime
) -> None:
    balance_cents = service.parse_amount_cents(acct.get("balance"))
    available_cents = service.parse_amount_cents(acct.get("available-balance"))
    if balance_cents is None and available_cents is None:
        return
    as_of = _epoch_to_dt(acct.get("balance-date")) or now
    snap_date = as_of.astimezone(tz).date()
    row = db.execute(
        select(BankFeedBalanceSnapshot).where(
            BankFeedBalanceSnapshot.account_id == account.id,
            BankFeedBalanceSnapshot.snapshot_date == snap_date,
        )
    ).scalar_one_or_none()
    if row is None:
        row = BankFeedBalanceSnapshot(account_id=account.id, snapshot_date=snap_date)
        db.add(row)
    # Last observation of the day wins.
    row.balance_cents = balance_cents
    row.available_balance_cents = available_cents
    row.balance_as_of = as_of
    row.updated_at = now


def _upsert_sfin_transaction(
    db: Session,
    account: BankFeedAccount,
    txn: dict,
    tz: ZoneInfo,
    now: datetime,
    existing_by_ext: dict[str, BankFeedTransaction],
) -> bool:
    """Returns True when a row was upserted. Pending rows are DROPPED, not
    stored — posted-only is the duplicate-safety contract."""
    ext_id = str(txn.get("id") or "")
    if not ext_id:
        return False
    if bool(txn.get("pending")):
        return False
    posted_at = _epoch_to_dt(txn.get("posted"))
    if posted_at is None:
        # posted==0 means "still pending" per spec — same drop rule.
        return False

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

    amount_cents = service.parse_amount_cents(txn.get("amount"))
    if amount_cents is not None or row.amount_cents is None:
        row.amount_cents = amount_cents
    row.pending = False
    row.posted_at = posted_at
    row.posted_date = posted_at.astimezone(tz).date()
    description = str(txn.get("description") or "")
    payee = str(txn.get("payee") or "")
    row.payee = (payee or description)[:300] or row.payee
    row.memo = str(txn.get("memo") or "") or description or row.memo
    if payee:
        row.merchant_name = payee[:300]
    mcc = str(txn.get("mcc") or "")
    if mcc:
        row.txn_subtype = f"mcc:{mcc}"[:50]
    # Same hash basis as the Banno path (account|date|cents|normalized memo)
    # so the GL fold-in never has to care which provider wrote the row.
    row.line_hash = service.compute_line_hash(
        account.external_account_id, row.posted_date, row.amount_cents, {"memo": description}
    )
    if row.deleted_at is not None:
        row.deleted_at = None
    row.raw_json = txn
    row.last_synced_at = now
    row.updated_at = now
    return True


# ── errlist handling ───────────────────────────────────────────────────


def _classify_errlist(payload: dict) -> tuple[bool, set[str], list[str]]:
    """(auth_broken, failed_account_ids, user_messages).

    Any ``gen.``/``con.`` non-auth error also poisons every watermark this
    run (we can't know which accounts the incomplete data belongs to), but
    received rows are still upserted — upserts are idempotent.
    """
    auth_broken = False
    failed_accounts: set[str] = set()
    messages: list[str] = []
    entries = payload.get("errlist")
    if not isinstance(entries, list):
        # Deprecated v1 shape: plain strings in "errors".
        entries = [{"code": "gen.", "msg": str(m)} for m in (payload.get("errors") or [])]
    for err in entries:
        if not isinstance(err, dict):
            continue
        code = str(err.get("code") or "gen.")
        msg = str(err.get("msg") or "")[:300]
        if msg:
            messages.append(msg)
        if code.startswith(("gen.auth", "con.auth")):
            auth_broken = True
        elif code.startswith("act."):
            failed_accounts.add(str(err.get("account_id") or ""))
        elif code.startswith(("gen.", "con.")):
            failed_accounts.add("*")
    return auth_broken, failed_accounts, messages


# ── the sync ───────────────────────────────────────────────────────────


def _epoch_of(d: date) -> int:
    return int(datetime.combine(d, dt_time.min, tzinfo=timezone.utc).timestamp())


def _ingest_payload(
    db: Session, connection: BannoConnection, payload: dict, tz: ZoneInfo, now: datetime,
    accounts_cache: dict[str, BankFeedAccount],
    txn_cache: dict[str, dict[str, BankFeedTransaction]],
) -> tuple[dict, set[str]]:
    """Returns (stats, present_ids). ``present_ids`` is every account id the
    payload actually carried — watermarks may only advance for members
    (an account MX omitted this run must not be stamped "synced")."""
    stats = {"accounts": 0, "upserted": 0, "dropped_pending": 0}
    present: set[str] = set()
    for acct in payload.get("accounts") or []:
        row = _upsert_sfin_account(db, connection, acct, tz, now, accounts_cache)
        if row is None:
            continue
        stats["accounts"] += 1
        present.add(row.external_account_id)
        db.flush()  # account id needed for txn/snapshot FKs on first sight
        # sync_enabled=False / inactive: metadata refreshes above, but no
        # transactions and no snapshots — the toggle must actually stop
        # ingestion (one GET returns every account regardless).
        if not row.sync_enabled or row.is_inactive:
            continue
        _upsert_balance_snapshot(db, row, acct, tz, now)
        per_acct = txn_cache.setdefault(row.external_account_id, {})
        for txn in acct.get("transactions") or []:
            if _upsert_sfin_transaction(db, row, txn, tz, now, per_acct):
                stats["upserted"] += 1
            else:
                stats["dropped_pending"] += 1
    return stats, present


_DEFAULT_SFIN_LABELS = {"SimpleFIN Bridge", "SimpleFIN", "beta-bridge.simplefin.org", "bridge.simplefin.org"}


def promote_org_label(db: Session, institution: BannoInstitution) -> bool:
    """Name the institution after the BANK, not the pipe (Doug 2026-08-16:
    "isn't that where the information came from — how do we know what bank
    it is"). SimpleFIN's per-account ``org`` carries the real institution
    (name/domain); it already rides in ``bank_feed_accounts.raw_json``.
    Only replaces the generic bridge labels — an operator rename sticks."""
    if (institution.display_label or "") not in _DEFAULT_SFIN_LABELS:
        return False
    connection = get_simplefin_connection(db, institution)
    if connection is None:
        return False
    for account in db.execute(
        select(BankFeedAccount).where(BankFeedAccount.connection_id == connection.id)
    ).scalars().all():
        org = (account.raw_json or {}).get("org") or {}
        name = str(org.get("name") or org.get("domain") or "").strip()
        if name:
            institution.display_label = name[:120]
            return True
    return False


def sync_institution(db: Session, institution: BannoInstitution) -> dict:
    """Entry point called from ``_sync_one_institution`` for provider rows.
    Mirrors the Banno path's result contract ({institution_id, errors:[…]}).
    """
    result: dict = {"institution_id": str(institution.id), "provider": PROVIDER_SIMPLEFIN, "errors": []}
    connection = get_simplefin_connection(db, institution)
    if connection is None or connection.auth_state == AUTH_DISCONNECTED:
        result["skipped_no_connection"] = True
        return result
    if connection.auth_state != AUTH_HEALTHY:
        result["errors"].append({"connection": str(connection.id), "skipped_unhealthy": True})
        return result

    # Runs on stored raw_json, so an institution still carrying the generic
    # bridge label heals on the NEXT sync — no re-claim needed.
    if promote_org_label(db, institution):
        result["label_promoted"] = institution.display_label

    schedule = service.get_or_create_schedule(db)
    tz = service.tenant_zoneinfo(db)
    now = _utcnow()
    today_local = now.astimezone(tz).date()
    quota = quota_state(db, schedule, tz)
    if quota["remaining"] <= 0:
        result["skipped_quota"] = True
        return result

    accounts = db.execute(
        select(BankFeedAccount).where(BankFeedAccount.connection_id == connection.id)
    ).scalars().all()
    enabled = [a for a in accounts if a.sync_enabled and not a.is_inactive]
    if accounts and not enabled:
        # Every account is disabled/inactive: fetching would burn quota on
        # data nothing may ingest, and with no watermark ever advancing the
        # horizon walk would repeat EVERY run (~5 fetches/day, forever).
        result["skipped_all_disabled"] = True
        return result
    resync_requested = False
    for a in enabled:
        if a.full_resync_required:
            a.initial_backfill_done = False
            a.backfill_synced_through = None
            a.full_resync_required = False
            resync_requested = True
    if resync_requested:
        db.commit()

    watermarks = [a.backfill_synced_through for a in enabled]
    in_backfill = (
        not accounts
        or not enabled
        or any(w is None for w in watermarks)
        or any(not a.initial_backfill_done for a in enabled)
    )
    horizon = today_local - timedelta(days=int(schedule.backfill_days or 365))

    if in_backfill:
        result["mode"] = "backfill"
        known = [w for w in watermarks if w is not None]
        if known and len(known) == len(watermarks) and len(watermarks) > 0:
            # Every account has a committed window — resume from the least
            # advanced one (a died/quota-stopped run picks up here).
            start_date = max(horizon, min(known) - timedelta(days=1))
        else:
            # First run, a brand-new account, or a requested resync: walk
            # the whole horizon. Idempotent upserts make overlap free.
            start_date = horizon
    else:
        result["mode"] = "incremental"
        overlap_floor = min(w for w in watermarks) - timedelta(days=FETCH_OVERLAP_DAYS)
        start_date = max(horizon, overlap_floor)

    accounts_cache: dict[str, BankFeedAccount] = {a.external_account_id: a for a in accounts}
    txn_cache: dict[str, dict[str, BankFeedTransaction]] = {}
    totals = {"accounts": 0, "upserted": 0, "dropped_pending": 0}
    auth_broken = False
    failed_accounts: set[str] = set()
    messages: list[str] = []
    fetches = 0
    # Holder so the except paths can read the TRUE request count — retries
    # inside a failed call are real requests against the bridge's quota;
    # under-recording them is how a token gets disabled.
    client_ref: list[SimpleFINClient] = []

    try:
        with build_client(connection) as client:
            client_ref.append(client)
            window_start = start_date
            while True:
                window_end = min(window_start + timedelta(days=WINDOW_DAYS), today_local)
                remaining = effective_cap(schedule) - int(schedule.fetch_count_today or 0) - fetches
                if remaining <= 0:
                    result["stopped_quota"] = True
                    break
                payload = client.get_accounts(
                    start_date=_epoch_of(window_start),
                    # end-date is EXCLUSIVE — +1 day includes window_end itself.
                    end_date=_epoch_of(window_end + timedelta(days=1)),
                )
                fetches = client.requests_made
                broken, failed, msgs = _classify_errlist(payload)
                auth_broken = auth_broken or broken
                failed_accounts |= failed
                messages.extend(msgs)
                stats, present_ids = _ingest_payload(
                    db, connection, payload, tz, now, accounts_cache, txn_cache
                )
                for key in totals:
                    totals[key] += stats[key]
                if auth_broken:
                    break
                # Advance per-account watermarks — ONLY for accounts that
                # (a) were actually in this payload, (b) aren't flagged by an
                # act.* error or a run-wide "*" poison, and (c) aren't a new
                # account first seen mid-INCREMENTAL (it only got the overlap
                # window's few days; leaving its watermark unset flips the
                # next run into backfill mode so its full history is pulled).
                run_poisoned = "*" in failed_accounts
                for a in accounts_cache.values():
                    if not (a.sync_enabled and not a.is_inactive):
                        continue
                    if a.external_account_id not in present_ids:
                        continue
                    if run_poisoned or a.external_account_id in failed_accounts:
                        continue
                    if not in_backfill and not a.initial_backfill_done:
                        continue
                    a.backfill_synced_through = window_end
                    if window_end >= today_local:
                        a.initial_backfill_done = True
                    a.last_synced_at = now
                    a.updated_at = now
                db.commit()
                if window_end >= today_local:
                    break
                window_start = window_end - timedelta(days=FETCH_OVERLAP_DAYS)
    except SimpleFINAuthError as exc:
        auth_broken = True
        messages.append(str(exc))
        if isinstance(getattr(exc, "status_code", None), int):
            result["status_code"] = exc.status_code
        fetches = client_ref[0].requests_made if client_ref else 0
    except SimpleFINError as exc:
        messages.append(str(exc))
        result["errors"].append({"connection": str(connection.id), "error_class": exc.__class__.__name__})
        fetches = client_ref[0].requests_made if client_ref else 0
    finally:
        record_fetches(db, schedule, fetches, tz)

    if auth_broken:
        connection.auth_state = AUTH_NEEDS_RECONNECT
        connection.updated_at = _utcnow()
        db.commit()
        result["errors"].append({"connection": str(connection.id), "auth": "needs_reconnect"})
    for acct_id in sorted(failed_accounts - {"*"}):
        result["errors"].append({"account": acct_id, "incomplete": True})
    if "*" in failed_accounts:
        result["errors"].append({"connection": str(connection.id), "incomplete_run": True})
    if messages:
        # Spec: errlist messages must reach the user. They travel in this
        # result → record_scheduled_run → schedule.last_run_error, which
        # /simplefin/status returns and the Integrations card displays.
        result["messages"] = messages[:5]
    result["stats"] = totals
    result["fetches"] = fetches
    return result
