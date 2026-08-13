"""SimpleFIN endpoints — /api/bank-feeds/simplefin/*.

Included by the bank_feeds router (same module gate + permissions). The UI
lives on the Settings → Integrations card (Doug 2026-08-13), not the Bank
Feeds view — but the data it manages renders in the existing Bank Feeds
tabs like any other provider.

Claim transactionality (the plan's rule): the setup token is ONE-TIME-USE,
so the claimed Access URL is persisted (committed) before anything else —
the account preview fetch afterwards may fail freely without burning the
credential.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request as FastAPIRequest
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.modules.bank_feeds import service, simplefin_service
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    DAILY_FETCH_CAP_MAX,
    PROVIDER_SIMPLEFIN,
    VALID_FREQUENCIES,
    BankFeedAccount,
    BankFeedBalanceSnapshot,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.bank_feeds.simplefin_client import (
    SimpleFINClaimError,
    SimpleFINError,
    claim_access_url,
)
from gdx_dispatch.modules.bank_feeds.statement_models import BankStatementLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

sfin_router = APIRouter(tags=["bank_feeds_simplefin"])

_MODULE = Depends(require_module("bank_feeds"))
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

SIMPLEFIN_USER_ID = "simplefin-bridge"  # synthesized `sub` for the NOT NULL column


def _get_institution(db: Session) -> BannoInstitution | None:
    return simplefin_service.get_simplefin_institution(db)


def _require_connection(db: Session) -> tuple[BannoInstitution, BannoConnection]:
    inst = _get_institution(db)
    conn = simplefin_service.get_simplefin_connection(db, inst) if inst else None
    if inst is None or conn is None:
        raise HTTPException(status_code=404, detail="SimpleFIN is not connected")
    return inst, conn


# ── status ─────────────────────────────────────────────────────────────


@sfin_router.get("/status", dependencies=[_MODULE])
def simplefin_status(
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    inst = _get_institution(db)
    conn = simplefin_service.get_simplefin_connection(db, inst) if inst else None
    schedule = service.get_or_create_schedule(db)
    tz = service.tenant_zoneinfo(db)
    quota = simplefin_service.quota_state(db, schedule, tz)

    accounts: list[dict] = []
    last_synced: datetime | None = None
    oldest_enabled_sync: datetime | None = None
    has_enabled_never_synced = False
    backfill_done = True
    if conn is not None:
        rows = db.execute(
            select(BankFeedAccount).where(BankFeedAccount.connection_id == conn.id)
        ).scalars().all()
        for a in rows:
            ls = a.last_synced_at
            if ls is not None and ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            if ls and (last_synced is None or ls > last_synced):
                last_synced = ls
            if a.sync_enabled and not a.is_inactive:
                # Staleness must track the WORST enabled account, not the
                # best — one frozen account (persistent act.failed) with a
                # healthy sibling is exactly the silent case to surface.
                if ls is None:
                    has_enabled_never_synced = True
                elif oldest_enabled_sync is None or ls < oldest_enabled_sync:
                    oldest_enabled_sync = ls
                if not a.initial_backfill_done:
                    backfill_done = False
            accounts.append({
                "id": str(a.id),
                "external_account_id": a.external_account_id,
                "name": a.name,
                "account_number_masked": a.account_number_masked,
                "sync_enabled": a.sync_enabled,
                "is_inactive": a.is_inactive,
                "initial_backfill_done": a.initial_backfill_done,
                "backfill_synced_through": (
                    a.backfill_synced_through.isoformat() if a.backfill_synced_through else None
                ),
            })

    connected = conn is not None and conn.auth_state != AUTH_DISCONNECTED
    stale_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=simplefin_service.STALE_AFTER_HOURS
    )
    stale = bool(
        connected
        and conn.auth_state == AUTH_HEALTHY
        and (
            (oldest_enabled_sync is not None and oldest_enabled_sync < stale_cutoff)
            or (has_enabled_never_synced and last_synced is not None)
        )
    )
    return {
        "connected": connected,
        "auth_state": conn.auth_state if conn else None,
        "bridge_host": (conn.provider_base_url or "").split("//")[-1].split("/")[0] if conn else None,
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "stale": stale,
        "stale_after_hours": simplefin_service.STALE_AFTER_HOURS,
        "quota": quota,
        "schedule": {
            **service.schedule_dict(schedule),
            "fetch_window_start": schedule.fetch_window_start,
            "fetch_window_end": schedule.fetch_window_end,
            "daily_fetch_cap": simplefin_service.effective_cap(schedule),
            "daily_fetch_cap_max": DAILY_FETCH_CAP_MAX,
        },
        "backfill_done": backfill_done,
        "accounts": accounts,
        "timezone": str(tz),
    }


# ── connect (claim) + re-link ──────────────────────────────────────────


class ConnectIn(BaseModel):
    setup_token: str = Field(min_length=8, max_length=4096)


def _propose_matches(existing_orphans: list[BankFeedAccount], incoming_new: list[dict]) -> list[dict]:
    """Name/mask-similarity proposals for the re-link step. Suggest-only —
    the user confirms; nothing is applied here."""
    proposals = []
    used: set[str] = set()
    for orphan in existing_orphans:
        best_id, best_score = None, 0.0
        o_name = (orphan.name or "").strip().lower()
        o_mask = (orphan.account_number_masked or "").lstrip("•")
        for cand in incoming_new:
            cid = str(cand.get("id") or "")
            if cid in used:
                continue
            c_name = str(cand.get("name") or "").strip().lower()
            score = 0.0
            if o_name and c_name and o_name == c_name:
                score = 1.0
            elif o_name and c_name and (o_name in c_name or c_name in o_name):
                score = 0.6
            if o_mask and o_mask in str(cand.get("name") or ""):
                score = max(score, 0.8)
            if score > best_score:
                best_id, best_score = cid, score
        if best_id and best_score >= 0.6:
            used.add(best_id)
            proposals.append({
                "account_id": str(orphan.id),
                "account_name": orphan.name,
                "new_external_id": best_id,
                "confidence": best_score,
            })
    return proposals


@sfin_router.post("/connect", dependencies=[_MODULE])
def simplefin_connect(
    body: ConnectIn,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    from gdx_dispatch.modules.bank_feeds import oauth  # noqa: PLC0415
    from gdx_dispatch.modules.bank_feeds.router import _audit  # noqa: PLC0415

    try:
        base_url, username, password = claim_access_url(body.setup_token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except SimpleFINClaimError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except SimpleFINError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    # ── persist FIRST: the setup token is burned; losing this URL loses
    # the connection. Nothing below this commit may prevent storage. ──
    bridge_host = base_url.split("//", 1)[-1].split("/", 1)[0]
    inst = _get_institution(db)
    if inst is None:
        inst = BannoInstitution(
            provider=PROVIDER_SIMPLEFIN,
            fi_host=bridge_host,
            display_label="SimpleFIN Bridge",
        )
        db.add(inst)
        db.flush()
    conn = simplefin_service.get_simplefin_connection(db, inst)
    if conn is None:
        conn = BannoConnection(
            institution_id=inst.id,
            fi_host=bridge_host,
            banno_user_id=SIMPLEFIN_USER_ID,
        )
        db.add(conn)
    conn.fi_host = bridge_host
    conn.provider_base_url = base_url
    conn.access_token_enc = oauth._encrypt(f"{username}:{password}")
    conn.refresh_token_enc = None
    conn.auth_state = AUTH_HEALTHY
    conn.connected_by_user_id = str(current_user.get("sub") or current_user.get("user_id") or "")
    conn.updated_at = datetime.now(timezone.utc)
    inst.fi_host = bridge_host
    db.commit()
    _audit(db, request, current_user, "bank_feeds_simplefin_connected", str(inst.id))

    # ── best-effort preview for the re-link step (claim already safe) ──
    preview: dict | None = None
    warning: str | None = None
    schedule = service.get_or_create_schedule(db)
    tz = service.tenant_zoneinfo(db)
    client_ref: list = []
    try:
        with simplefin_service.build_client(conn) as client:
            client_ref.append(client)
            payload = client.get_accounts(balances_only=True)
        incoming = [
            {"id": str(a.get("id") or ""), "name": str(a.get("name") or ""),
             "balance": str(a.get("balance") or "")}
            for a in (payload.get("accounts") or []) if a.get("id")
        ]
        incoming_ids = {a["id"] for a in incoming}
        existing = db.execute(
            select(BankFeedAccount).where(BankFeedAccount.connection_id == conn.id)
        ).scalars().all()
        orphans = [a for a in existing if a.external_account_id not in incoming_ids]
        known_ids = {a.external_account_id for a in existing}
        fresh = [a for a in incoming if a["id"] not in known_ids]
        preview = {
            "incoming": incoming,
            "orphaned": [
                {"account_id": str(a.id), "name": a.name,
                 "external_account_id": a.external_account_id}
                for a in orphans
            ],
            "new": fresh,
            "proposals": _propose_matches(orphans, fresh),
        }
    except Exception as exc:  # noqa: BLE001 — claim is safe; preview is optional
        log.warning("simplefin_preview_failed err=%s", exc.__class__.__name__)
        warning = "Connected, but the account preview failed — run Sync Now to load accounts."
    finally:
        # Failed previews made real bridge requests too — the ledger counts
        # every request or the 20-vs-24 headroom is fiction.
        if client_ref:
            simplefin_service.record_fetches(db, schedule, client_ref[0].requests_made, tz)

    return {"connected": True, "bridge_host": bridge_host, "preview": preview, "warning": warning}


def _absorb_duplicate_account(db: Session, *, keeper: BankFeedAccount, dup: BankFeedAccount) -> None:
    """Fold a younger duplicate account row into the original.

    Transactions/snapshots the keeper already holds (same external txn id /
    same snapshot date) are the same real records re-fetched under the new
    account id — the keeper's copy wins and the duplicate's copy is deleted.
    Everything else moves. The duplicate row is deleted and flushed BEFORE
    the caller re-points the keeper, so the unique
    (connection_id, external_account_id) key is free at flush time.
    """
    keeper_txn_ids = {
        r
        for (r,) in db.execute(
            select(BankFeedTransaction.external_transaction_id).where(
                BankFeedTransaction.account_id == keeper.id
            )
        )
    }
    for txn in db.execute(
        select(BankFeedTransaction).where(BankFeedTransaction.account_id == dup.id)
    ).scalars():
        if txn.external_transaction_id in keeper_txn_ids:
            db.delete(txn)
        else:
            txn.account_id = keeper.id
    keeper_snap_dates = {
        d
        for (d,) in db.execute(
            select(BankFeedBalanceSnapshot.snapshot_date).where(
                BankFeedBalanceSnapshot.account_id == keeper.id
            )
        )
    }
    for snap in db.execute(
        select(BankFeedBalanceSnapshot).where(BankFeedBalanceSnapshot.account_id == dup.id)
    ).scalars():
        if snap.snapshot_date in keeper_snap_dates:
            db.delete(snap)
        else:
            snap.account_id = keeper.id
    db.delete(dup)
    db.flush()


class RelinkMapping(BaseModel):
    account_id: str
    new_external_id: str = Field(min_length=1, max_length=120)


class RelinkIn(BaseModel):
    mappings: list[RelinkMapping]


@sfin_router.post("/relink", dependencies=[_MODULE])
def simplefin_relink(
    body: RelinkIn,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    """Re-point existing account rows at new external ids after a reconnect
    presented the same bank accounts under different ids. Keeps every
    transaction and watermark.

    The reconnect race: if a sync (scheduled beat or Sync Now) runs between
    /connect and this call, it mints a FRESH BankFeedAccount under the new
    external id. That younger row is absorbed — its transactions/snapshots
    move onto the original (duplicates of rows the original already holds
    are the same real transactions re-fetched, so they're dropped), then the
    duplicate row is deleted and the original is re-pointed. A collision
    with an OLDER row is genuine ambiguity and still refuses with 409.
    """
    from gdx_dispatch.modules.bank_feeds.router import _audit  # noqa: PLC0415

    _, conn = _require_connection(db)
    by_ext = {
        a.external_account_id: a
        for a in db.execute(
            select(BankFeedAccount).where(BankFeedAccount.connection_id == conn.id)
        ).scalars()
    }
    updated = []
    absorbed = []
    for m in body.mappings:
        try:
            row = db.get(BankFeedAccount, UUID(m.account_id))
        except ValueError:
            row = None
        if row is None or row.connection_id != conn.id:
            raise HTTPException(status_code=404, detail=f"Account not found: {m.account_id}")
        if m.new_external_id == row.external_account_id:
            continue
        dup = by_ext.get(m.new_external_id)
        if dup is not None and dup.id != row.id:
            if dup.created_at <= row.created_at:
                raise HTTPException(
                    status_code=409,
                    detail=f"External id already linked to another account: {m.new_external_id}",
                )
            _absorb_duplicate_account(db, keeper=row, dup=dup)
            absorbed.append(str(dup.id))
            by_ext.pop(m.new_external_id, None)
        by_ext.pop(row.external_account_id, None)
        row.external_account_id = m.new_external_id
        row.updated_at = datetime.now(timezone.utc)
        by_ext[m.new_external_id] = row
        updated.append(str(row.id))
    db.commit()
    _audit(db, request, current_user, "bank_feeds_simplefin_relinked", ",".join(updated) or "-")
    return {"updated": updated, "absorbed": absorbed}


# ── settings ───────────────────────────────────────────────────────────


class SimplefinSettingsIn(BaseModel):
    frequency: str | None = None
    fetch_window_start: str | None = None
    fetch_window_end: str | None = None
    daily_fetch_cap: int | None = Field(default=None, ge=1, le=DAILY_FETCH_CAP_MAX)
    clear_fetch_window: bool = False


@sfin_router.put("/settings", dependencies=[_MODULE])
def simplefin_settings(
    body: SimplefinSettingsIn,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    from gdx_dispatch.modules.bank_feeds.router import _audit  # noqa: PLC0415

    schedule = service.get_or_create_schedule(db)
    if body.frequency is not None:
        if body.frequency not in VALID_FREQUENCIES:
            raise HTTPException(status_code=422, detail="Invalid frequency")
        service.update_schedule(db, body.frequency)
        schedule = service.get_or_create_schedule(db)
    if body.clear_fetch_window:
        schedule.fetch_window_start = None
        schedule.fetch_window_end = None
    elif body.fetch_window_start is not None or body.fetch_window_end is not None:
        start = simplefin_service.parse_hhmm(body.fetch_window_start)
        end = simplefin_service.parse_hhmm(body.fetch_window_end)
        if start is None or end is None:
            raise HTTPException(
                status_code=422,
                detail="Fetch window needs both start and end as HH:MM (local time)",
            )
        if start == end:
            raise HTTPException(status_code=422, detail="Fetch window start and end must differ")
        schedule.fetch_window_start = body.fetch_window_start
        schedule.fetch_window_end = body.fetch_window_end
    if body.daily_fetch_cap is not None:
        schedule.daily_fetch_cap = min(int(body.daily_fetch_cap), DAILY_FETCH_CAP_MAX)
    schedule.updated_at = datetime.now(timezone.utc)
    db.commit()
    _audit(db, request, current_user, "bank_feeds_simplefin_settings", "-")
    tz = service.tenant_zoneinfo(db)
    return {
        **service.schedule_dict(schedule),
        "fetch_window_start": schedule.fetch_window_start,
        "fetch_window_end": schedule.fetch_window_end,
        "daily_fetch_cap": simplefin_service.effective_cap(schedule),
        "daily_fetch_cap_max": DAILY_FETCH_CAP_MAX,
        "timezone": str(tz),
    }


# ── manual sync + disconnect ───────────────────────────────────────────


@sfin_router.post("/sync", dependencies=[_MODULE])
def simplefin_sync_now(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    """Manual fetch. Bypasses the fetch-hours window (an explicit human ask)
    but NEVER the daily cap — scheduled and manual share one ledger."""
    from gdx_dispatch.modules.bank_feeds.router import _tenant_id  # noqa: PLC0415
    from gdx_dispatch.modules.bank_feeds.tasks import bank_feeds_sync_task  # noqa: PLC0415

    inst, _conn = _require_connection(db)
    schedule = service.get_or_create_schedule(db)
    tz = service.tenant_zoneinfo(db)
    quota = simplefin_service.quota_state(db, schedule, tz)
    if quota["remaining"] <= 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily fetch cap reached ({quota['used']}/{quota['cap']}). "
                "The counter resets at local midnight."
            ),
        )
    bank_feeds_sync_task.delay(_tenant_id(request), False, str(inst.id))
    return {"queued": True, "quota": quota}


@sfin_router.post("/disconnect", dependencies=[_MODULE])
def simplefin_disconnect(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    from gdx_dispatch.modules.bank_feeds import oauth  # noqa: PLC0415
    from gdx_dispatch.modules.bank_feeds.router import _audit  # noqa: PLC0415

    inst, _conn = _require_connection(db)
    count = oauth.soft_disconnect(db, inst.id)
    _audit(db, request, current_user, "bank_feeds_simplefin_disconnected", str(inst.id))
    return {"disconnected": count}


# ── balance history (the cash curve) ───────────────────────────────────


@sfin_router.get("/balance-history", dependencies=[_MODULE])
def simplefin_balance_history(
    account_id: str,
    days: int = 90,
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    try:
        acct_uuid = UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Account not found") from None
    days = max(1, min(int(days), 730))
    floor = date.today() - timedelta(days=days)
    rows = db.execute(
        select(BankFeedBalanceSnapshot)
        .where(
            BankFeedBalanceSnapshot.account_id == acct_uuid,
            BankFeedBalanceSnapshot.snapshot_date >= floor,
        )
        .order_by(BankFeedBalanceSnapshot.snapshot_date.asc())
    ).scalars().all()
    return {
        "account_id": account_id,
        "snapshots": [
            {
                "date": r.snapshot_date.isoformat(),
                "balance_cents": r.balance_cents,
                "available_balance_cents": r.available_balance_cents,
            }
            for r in rows
        ],
    }


# ── feed ↔ statement tie-out ───────────────────────────────────────────


def _month_bounds(month: str) -> tuple[date, date]:
    if not _MONTH_RE.match(month or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = (date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)) - timedelta(days=1)
    return first, last


@sfin_router.get("/tieout", dependencies=[_MODULE])
def simplefin_tieout(
    feed_account_id: str,
    bank_account_id: str,
    month: str,
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    """Month tie-out: the statement evidence and the feed should agree; a
    mismatch is an error or fraud signal. ±1-day date tolerance because the
    feed's epoch→local posted_date can differ from the statement's posting
    date. Suggest-only report — mutates nothing.
    """
    first, last = _month_bounds(month)
    try:
        feed_uuid = UUID(feed_account_id)
        stmt_uuid = UUID(bank_account_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Account not found") from None

    stmt_lines = db.execute(
        select(BankStatementLine).where(
            BankStatementLine.bank_account_id == stmt_uuid,
            BankStatementLine.txn_date >= first,
            BankStatementLine.txn_date <= last,
        ).order_by(BankStatementLine.txn_date.asc())
    ).scalars().all()
    feed_rows = db.execute(
        select(BankFeedTransaction).where(
            BankFeedTransaction.account_id == feed_uuid,
            BankFeedTransaction.posted_date >= first - timedelta(days=1),
            BankFeedTransaction.posted_date <= last + timedelta(days=1),
            BankFeedTransaction.amount_cents.is_not(None),
            BankFeedTransaction.pending.is_(False),
            BankFeedTransaction.deleted_at.is_(None),
        ).order_by(BankFeedTransaction.posted_date.asc())
    ).scalars().all()

    # Greedy amount-bucket matching, each side used once, date distance ≤1.
    feed_by_amount: dict[int, list[BankFeedTransaction]] = {}
    for f in feed_rows:
        feed_by_amount.setdefault(f.amount_cents, []).append(f)
    matched = 0
    unmatched_stmt = []
    used_feed: set[str] = set()
    for line in stmt_lines:
        best = None
        for cand in feed_by_amount.get(line.amount_cents, []):
            if str(cand.id) in used_feed or cand.posted_date is None:
                continue
            if abs((cand.posted_date - line.txn_date).days) <= 1:
                best = cand
                break
        if best is not None:
            used_feed.add(str(best.id))
            matched += 1
        else:
            unmatched_stmt.append({
                "id": str(line.id),
                "date": line.txn_date.isoformat(),
                "amount_cents": line.amount_cents,
                "description": (line.description or "")[:120],
            })
    unmatched_feed = [
        {
            "id": str(f.id),
            "date": f.posted_date.isoformat() if f.posted_date else None,
            "amount_cents": f.amount_cents,
            "description": (f.memo or f.payee or "")[:120],
        }
        for f in feed_rows
        if str(f.id) not in used_feed
        and f.posted_date is not None
        and first <= f.posted_date <= last
    ]
    return {
        "month": month,
        "statement_lines": len(stmt_lines),
        "feed_transactions": sum(
            1 for f in feed_rows if f.posted_date and first <= f.posted_date <= last
        ),
        "matched": matched,
        "statement_only": unmatched_stmt,
        "feed_only": unmatched_feed,
        "statement_sum_cents": sum(line.amount_cents for line in stmt_lines),
        "feed_sum_cents": sum(
            f.amount_cents for f in feed_rows
            if f.posted_date and first <= f.posted_date <= last
        ),
    }
