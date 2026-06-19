"""Banking module: full QuickBooks banking visibility.

Three QBO entities feed the unified Banking view:
  * Purchase (already pulled by sync.pull_bank_transactions) — expense side.
  * Deposit — income side (customer payments deposited, bank deposits).
  * Transfer — between two accounts on the same chart.

Plus account balances (qb_accounts.current_balance, refreshed via
sync.pull_accounts) and a per-tenant sync schedule.

All tables live on the tenant plane (per three-plane isolation: isolation
is the connection, no tenant_id filter columns).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    JSON,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.models.tenant_models import TenantBase
from gdx_dispatch.modules.quickbooks.client import QBAPIError, QBClient

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────
# Models — tenant plane
# ─────────────────────────────────────────────────────────────────────


class QBDeposit(TenantBase):
    """Mirror of QBO Deposit entity (one row per Deposit transaction).

    Aggregated amount only — individual DepositLineDetail rows kept in
    raw_json for forward compatibility. Bank-visibility view doesn't
    need line-level breakdown today.

    Soft-delete: ``deleted_at`` is set by the sync reconciler when a
    row that was previously synced is no longer returned by QBO's
    query (within the synced date window). The unified-feed read
    filters ``deleted_at IS NULL`` — ghost rows stay in the table for
    audit but disappear from the user view.
    """

    __tablename__ = "qb_deposits"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    qb_txn_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    deposit_to_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    deposit_to_account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Comma-separated "TxnType:TxnId" pairs (e.g. "Payment:123,SalesReceipt:456")
    # extracted from raw_json.Line[].LinkedTxn[] at sync time. Surfaced in the
    # unified feed as `linked_txn_ids` so the user can see which Payment(s)
    # this Deposit swept from Undeposited Funds — answers the "is this a
    # double-count?" question without needing to crack raw_json on read.
    linked_qb_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class QBBankingEntry(TenantBase):
    """Unified mirror for the 'other' QBO entities that hit bank/CC accounts.

    Covers BillPayment, SalesReceipt, RefundReceipt, CreditCardCredit,
    and JournalEntry (one entry row per JE line that posts to a bank
    or credit-card account). Existing Purchase / Deposit / Transfer
    stay in their dedicated tables (qb_bank_transactions / qb_deposits
    / qb_transfers) for backward compatibility — the unified feed
    UNIONs all four sources.

    Why one table for five entities? Each has different QBO schema
    but the same banking-visibility shape: date, account, counterparty,
    signed amount, memo. Schema-on-read via entity_type + raw_json
    keeps the model count manageable.

    `qb_entity` carries the QBO entity name verbatim; `qb_line_index`
    is non-NULL only for JournalEntry rows (one JE → N entries, one
    per bank-touching line) — uniqueness is on (qb_entity, qb_txn_id,
    qb_line_index).
    """

    __tablename__ = "qb_banking_entries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    qb_entity: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    qb_txn_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    qb_line_index: Mapped[int] = mapped_column(Numeric(4, 0), nullable=False, default=0, server_default="0")
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)  # signed: -out / +in
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    counterparty_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class QBTransfer(TenantBase):
    """Mirror of QBO Transfer entity (funds moved between two accounts)."""

    __tablename__ = "qb_transfers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    qb_txn_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    from_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    from_account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    to_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    to_account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class QBBankTransaction(TenantBase):
    """Mirror of QBO Purchase entity (money-OUT side: vendor expense / CC charge).

    Promoted from inline DDL in sync.py to a real ORM model so the tenant
    plane matches the rest of banking — isolation via connection only, no
    tenant_id column. Old prod tables still carry a (now-unused) tenant_id
    column for backward compat; sync.pull_bank_transactions performs an
    additive ALTER (ADD COLUMN deleted_at IF NOT EXISTS, drop the legacy
    UNIQUE(tenant_id, qb_txn_id) in favor of UNIQUE(qb_txn_id)). The
    legacy column itself stays for one release cycle, then drops in a
    follow-up sprint after burn-in.

    Stored amount mirrors QBO TotalAmt (non-negative). The signed/direction
    flip happens at read time in unified_banking_transactions so historic
    data doesn't need a backfill — Purchases are always money-OUT by
    definition.

    Soft-delete: same reconciler pattern as qb_deposits/qb_transfers. QBO
    query() does NOT return deleted Purchases (CDC/webhooks would; we
    don't subscribe). After each pull, any row in the synced window whose
    qb_txn_id wasn't in the response is marked deleted_at = now().
    """

    __tablename__ = "qb_bank_transactions"

    # NOTE: __table_args__ removed deliberately — the legacy prod schema
    # has UNIQUE(tenant_id, qb_txn_id) baked in; we ALTER toward
    # UNIQUE(qb_txn_id) inside sync.py instead of asking SQLAlchemy to
    # create a competing constraint. The Mapped[str] qb_txn_id below
    # carries `unique=True` for fresh tenants where create_all runs.

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    qb_txn_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    txn_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payee: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


FREQ_MANUAL = "manual"
FREQ_HOURLY = "hourly"
FREQ_EVERY_4H = "every_4h"
FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
VALID_FREQUENCIES = (FREQ_MANUAL, FREQ_HOURLY, FREQ_EVERY_4H, FREQ_DAILY, FREQ_WEEKLY)


class QBSyncSchedule(TenantBase):
    """Singleton row per tenant DB. Drives the Celery beat dispatcher.

    Set frequency=manual to disable scheduled sync (the Sync Now button
    in the UI still works). last_run_at + next_run_at are managed by the
    dispatcher task.
    """

    __tablename__ = "qb_sync_schedule"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, default=FREQ_MANUAL, server_default=FREQ_MANUAL)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_run_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ─────────────────────────────────────────────────────────────────────
# Deposit + Transfer sync
# ─────────────────────────────────────────────────────────────────────


async def pull_deposits(
    tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "",
) -> dict[str, Any]:
    """Pull Deposit entity from QBO. Returns {created, updated, deleted, errors}.

    Reconciler: after the upsert, any qb_deposits row within the synced
    date window that was NOT in the QBO response gets soft-deleted
    (deleted_at = now). The unified-feed read filters these out.
    """
    where = _build_date_where(start_date, end_date)
    try:
        rows = await qb.query("Deposit", where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise

    # qb_deposits.linked_qb_ids may not exist on legacy tenant DBs — add it
    # additively before the first write. Safe (IF NOT EXISTS) on PG; SQLite
    # silently no-ops via the try/except since ALTER ADD COLUMN IF NOT EXISTS
    # isn't supported. Test fixtures don't define the column either.
    _ensure_deposit_linked_qb_ids_column(db)

    seen_qb_ids: set[str] = set()
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    for raw in rows:
        qb_id = str(raw.get("Id") or "").strip()
        if not qb_id:
            continue
        seen_qb_ids.add(qb_id)
        try:
            deposit_to_ref = raw.get("DepositToAccountRef") or {}
            row = db.execute(
                text("SELECT id FROM qb_deposits WHERE qb_txn_id = :qid"),
                {"qid": qb_id},
            ).first()
            # Extract LinkedTxn pairs from Line[]: each line carries
            # LinkedTxn[{TxnId, TxnType}] when the deposit swept funds from
            # Undeposited Funds. We collapse into "TxnType:TxnId" strings for
            # cheap display; raw_json keeps the structured form for any
            # future reconciliation pass.
            params = {
                "qid": qb_id,
                "td": _parse_date(raw.get("TxnDate")),
                "amt": float(raw.get("TotalAmt") or 0),
                "aid": str(deposit_to_ref.get("value") or "") or None,
                "aname": deposit_to_ref.get("name") or None,
                "memo": (raw.get("PrivateNote") or raw.get("Memo") or None),
                "linked": _extract_linked_txn_ids(raw),
                # Serialize JSON for the bind — psycopg2 can't adapt dict directly
                # to JSONB; SQLite tolerates either. Caught by the prod 500 on
                # GDX's first Transfer row 2026-05-20.
                "raw": json.dumps(raw),
            }
            if row:
                # Clear deleted_at on every re-sync: if a prior reconcile
                # tombstoned this row by mistake (transient empty page,
                # NULL date edge case, etc.), seeing it again in the
                # response un-tombstones it. The inverse operation makes
                # false positives recoverable.
                db.execute(text("""
                    UPDATE qb_deposits SET txn_date=:td, total_amount=:amt,
                        deposit_to_account_id=:aid, deposit_to_account_name=:aname,
                        memo=:memo, linked_qb_ids=:linked, raw_json=:raw,
                        last_synced_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP, deleted_at=NULL
                    WHERE qb_txn_id=:qid
                """), params)
                updated += 1
            else:
                params["id"] = str(uuid4())
                # Explicit timestamps — raw text() bypasses ORM default callables.
                db.execute(text("""
                    INSERT INTO qb_deposits (id, qb_txn_id, txn_date, total_amount,
                        deposit_to_account_id, deposit_to_account_name, memo,
                        linked_qb_ids, raw_json,
                        last_synced_at, created_at, updated_at)
                    VALUES (:id, :qid, :td, :amt, :aid, :aname, :memo,
                            :linked, :raw,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """), params)
                created += 1
        except Exception as exc:
            log.exception("qb_pull_deposits_row_failed qb_id=%s", qb_id)
            errors.append({"qb_id": qb_id, "error": str(exc)[:200]})
    db.commit()
    deleted = _reconcile_tombstones(db, "qb_deposits", seen_qb_ids, start_date, end_date)
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}


def _extract_linked_txn_ids(deposit_raw: dict[str, Any]) -> str | None:
    """Walk Deposit.Line[].LinkedTxn[] → "Payment:123,SalesReceipt:456".

    Returns None when no LinkedTxn entries present. De-dupes while preserving
    insertion order so display is stable across re-syncs.

    Defensive: the QBO contract says Line is a list of objects and each
    LinkedTxn is a list of objects with TxnType/TxnId. A garbage payload
    (string in place of list, list in place of object) shouldn't crash the
    sync — skip the bad shape and continue.
    """
    if not isinstance(deposit_raw, dict):
        return None
    pairs: list[str] = []
    seen: set[str] = set()
    lines = deposit_raw.get("Line") or []
    if not isinstance(lines, list):
        return None
    for line in lines:
        if not isinstance(line, dict):
            continue
        linked = line.get("LinkedTxn") or []
        if not isinstance(linked, list):
            continue
        for lt in linked:
            if not isinstance(lt, dict):
                continue
            txn_type = str(lt.get("TxnType") or "").strip()
            txn_id = str(lt.get("TxnId") or "").strip()
            if not txn_type or not txn_id:
                continue
            key = f"{txn_type}:{txn_id}"
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
    return ",".join(pairs) if pairs else None


def _ensure_deposit_linked_qb_ids_column(db: Session) -> None:
    """Additive ALTER for legacy tenants that pre-date the linked_qb_ids
    column. Idempotent on PG (ADD COLUMN IF NOT EXISTS); on SQLite (tests)
    we skip — fixtures create_all the table fresh from the ORM definition.
    """
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        try:
            db.execute(text(
                "ALTER TABLE qb_deposits ADD COLUMN IF NOT EXISTS linked_qb_ids TEXT"
            ))
            db.commit()
        except Exception:
            db.rollback()


# ─────────────────────────────────────────────────────────────────────
# Other bank-touching entities (BillPayment, SalesReceipt, RefundReceipt,
# CreditCardCredit, JournalEntry) — written to qb_banking_entries.
# ─────────────────────────────────────────────────────────────────────


def _upsert_banking_entry(
    db: Session,
    *,
    qb_entity: str,
    qb_txn_id: str,
    qb_line_index: int,
    txn_date: date | None,
    amount: float,
    account_id: str | None,
    account_name: str | None,
    counterparty_name: str | None,
    memo: str | None,
    raw_json: Any,
) -> str:
    """Returns 'created' or 'updated'."""
    existing = db.execute(text(
        "SELECT id FROM qb_banking_entries "
        "WHERE qb_entity = :qe AND qb_txn_id = :qid AND qb_line_index = :li"
    ), {"qe": qb_entity, "qid": qb_txn_id, "li": qb_line_index}).first()
    # Raw text() bypasses ORM type coercion — serialize JSON ourselves so the
    # value works on SQLite (TEXT) and Postgres (JSONB cast on the column).
    raw_serialized = json.dumps(raw_json) if raw_json is not None else None
    params = {
        "qe": qb_entity, "qid": qb_txn_id, "li": qb_line_index,
        "td": txn_date, "amt": amount,
        "aid": account_id, "aname": account_name,
        "cname": counterparty_name, "memo": memo, "raw": raw_serialized,
    }
    if existing:
        db.execute(text("""
            UPDATE qb_banking_entries SET txn_date=:td, amount=:amt,
                account_id=:aid, account_name=:aname,
                counterparty_name=:cname, memo=:memo, raw_json=:raw,
                last_synced_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                deleted_at=NULL
            WHERE qb_entity=:qe AND qb_txn_id=:qid AND qb_line_index=:li
        """), params)
        return "updated"
    params["id"] = str(uuid4())
    # Explicit timestamps: raw text() INSERT bypasses ORM's `default=`
    # callables, so the NOT NULL columns need CURRENT_TIMESTAMP here.
    db.execute(text("""
        INSERT INTO qb_banking_entries
            (id, qb_entity, qb_txn_id, qb_line_index, txn_date, amount,
             account_id, account_name, counterparty_name, memo, raw_json,
             last_synced_at, created_at, updated_at)
        VALUES (:id, :qe, :qid, :li, :td, :amt, :aid, :aname, :cname, :memo, :raw,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    """), params)
    return "created"


def _reconcile_entries_for(
    db: Session, qb_entity: str, seen_keys: set[tuple[str, int]],
    start_date: str, end_date: str,
) -> int:
    """Tombstone qb_banking_entries rows for this entity whose
    (qb_txn_id, qb_line_index) is not in seen_keys, scoped to date window."""
    params: dict[str, Any] = {"qe": qb_entity}
    where = ["qb_entity = :qe", "deleted_at IS NULL"]
    if start_date:
        where.append("(txn_date >= :sd OR txn_date IS NULL)")
        params["sd"] = start_date
    if end_date:
        where.append("(txn_date <= :ed OR txn_date IS NULL)")
        params["ed"] = end_date

    rows = db.execute(
        text(f"SELECT qb_txn_id, qb_line_index FROM qb_banking_entries WHERE " + " AND ".join(where)),
        params,
    ).all()
    to_tombstone = [(r[0], int(r[1])) for r in rows if (r[0], int(r[1])) not in seen_keys]
    if not to_tombstone:
        return 0
    for qid, li in to_tombstone:
        db.execute(text(
            "UPDATE qb_banking_entries SET deleted_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE qb_entity = :qe AND qb_txn_id = :qid AND qb_line_index = :li"
        ), {"qe": qb_entity, "qid": qid, "li": li})
    db.commit()
    return len(to_tombstone)


async def _pull_simple_entity(
    qb_entity: str, db: Session, qb: QBClient,
    start_date: str, end_date: str,
    *,
    counterparty_ref_key: str,
    account_ref_key: str,
    amount_sign: int,  # -1 = money out of bank, +1 = money in
) -> dict[str, Any]:
    """Generic pull for single-row-per-doc entities (BillPayment, SalesReceipt,
    RefundReceipt, CreditCardCredit). JournalEntry has its own loop.
    """
    where = _build_date_where(start_date, end_date)
    try:
        rows = await qb.query(qb_entity, where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise

    seen_keys: set[tuple[str, int]] = set()
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    for raw in rows:
        qb_id = str(raw.get("Id") or "").strip()
        if not qb_id:
            continue
        seen_keys.add((qb_id, 0))
        try:
            counter_ref = raw.get(counterparty_ref_key) or {}
            acct_ref = raw.get(account_ref_key) or {}
            # BillPayment is special: account ref nested under CheckPayment / CreditCardPayment.
            if qb_entity == "BillPayment" and not acct_ref:
                acct_ref = (raw.get("CheckPayment") or {}).get("BankAccountRef") or (
                    raw.get("CreditCardPayment") or {}
                ).get("CCAccountRef") or {}
            outcome = _upsert_banking_entry(
                db,
                qb_entity=qb_entity,
                qb_txn_id=qb_id,
                qb_line_index=0,
                txn_date=_parse_date(raw.get("TxnDate")),
                amount=float(raw.get("TotalAmt") or 0) * amount_sign,
                account_id=str(acct_ref.get("value") or "") or None,
                account_name=acct_ref.get("name") or None,
                counterparty_name=counter_ref.get("name") or None,
                memo=raw.get("PrivateNote") or raw.get("Memo") or None,
                raw_json=raw,
            )
            if outcome == "created":
                created += 1
            else:
                updated += 1
        except Exception as exc:
            log.exception("qb_pull_%s_row_failed qb_id=%s", qb_entity.lower(), qb_id)
            errors.append({"qb_id": qb_id, "error": str(exc)[:200]})
    db.commit()
    deleted = _reconcile_entries_for(db, qb_entity, seen_keys, start_date, end_date)
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}


async def pull_bill_payments(tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """Vendor bill payments. Money OUT of bank → negative amount."""
    return await _pull_simple_entity(
        "BillPayment", db, qb, start_date, end_date,
        counterparty_ref_key="VendorRef",
        account_ref_key="BankAccountRef",  # BillPayment's account is nested; _pull handles fallback.
        amount_sign=-1,
    )


async def pull_sales_receipts(tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """Instant sales (cash/check on the spot). Money IN to bank → positive."""
    return await _pull_simple_entity(
        "SalesReceipt", db, qb, start_date, end_date,
        counterparty_ref_key="CustomerRef",
        account_ref_key="DepositToAccountRef",
        amount_sign=+1,
    )


async def pull_refund_receipts(tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """Customer refunds. Money OUT of bank → negative amount."""
    return await _pull_simple_entity(
        "RefundReceipt", db, qb, start_date, end_date,
        counterparty_ref_key="CustomerRef",
        account_ref_key="DepositToAccountRef",  # actually the WITHDRAW-from account on a Refund.
        amount_sign=-1,
    )


# NOTE: there is NO `CreditCardCredit` entity in QuickBooks Online's API
# (that name is QBO Desktop terminology). QBO Online represents
# credit-card refunds/credits as `Purchase` rows with negative
# TotalAmt or as `RefundReceipt`. A previous version of this module
# wired pull_credit_card_credits → QBO 400 "invalid context
# declaration: CreditCardCredit" which 500'd the whole banking sync.
# If a future QBO API rev adds the entity, re-introduce here with a
# canary test that asserts QBO returns 200 for `SELECT * FROM
# CreditCardCredit`.


async def pull_customer_payments(
    tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "",
) -> dict[str, Any]:
    """Customer payments that land DIRECTLY in a bank account.

    QBO's Payment entity has two paths:
      * DepositToAccountRef set to a Bank/CC account → cash goes straight
        to the bank, we count it here.
      * DepositToAccountRef set to Undeposited Funds → cash sits in the
        clearing account until a Deposit txn moves it. The Deposit entity
        (already pulled) is what hits the bank — counting the Payment
        too would double-count.

    Filtered by qb_accounts.account_type IN {Bank, Credit Card}.
    """
    try:
        bank_ids = {
            r[0] for r in db.execute(text(
                "SELECT qb_account_id FROM qb_accounts WHERE account_type IN ('Bank', 'Credit Card')"
            )).all()
        }
    except (ProgrammingError, OperationalError):
        db.rollback()
        bank_ids = set()
    if not bank_ids:
        # Audit follow-up 2026-05-20: an upstream accounts-pull failure
        # used to land here as a silent zero. Now we surface it as an
        # error so the toast warns the user instead of pretending success.
        log.warning("qb_pull_customer_payments skipped — qb_accounts empty for tenant %s", tenant_id)
        return {"created": 0, "updated": 0, "deleted": 0, "errors": [{
            "qb_id": "*",
            "error": "skipped: qb_accounts empty — run accounts sync first to identify bank accounts",
        }]}

    where = _build_date_where(start_date, end_date)
    try:
        rows = await qb.query("Payment", where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise

    seen_keys: set[tuple[str, int]] = set()
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    for raw in rows:
        qb_id = str(raw.get("Id") or "").strip()
        if not qb_id:
            continue
        deposit_ref = raw.get("DepositToAccountRef") or {}
        acct_id = str(deposit_ref.get("value") or "")
        if not acct_id or acct_id not in bank_ids:
            # Either Undeposited Funds (Deposit txn handles it) or no
            # account ref at all — skip.
            continue
        seen_keys.add((qb_id, 0))
        try:
            customer_ref = raw.get("CustomerRef") or {}
            outcome = _upsert_banking_entry(
                db,
                qb_entity="Payment",
                qb_txn_id=qb_id,
                qb_line_index=0,
                txn_date=_parse_date(raw.get("TxnDate")),
                amount=float(raw.get("TotalAmt") or 0),  # +amount: money IN to bank
                account_id=acct_id,
                account_name=deposit_ref.get("name") or None,
                counterparty_name=customer_ref.get("name") or None,
                memo=raw.get("PrivateNote") or None,
                raw_json=raw,
            )
            if outcome == "created":
                created += 1
            else:
                updated += 1
        except Exception as exc:
            log.exception("qb_pull_customer_payments_row_failed qb_id=%s", qb_id)
            errors.append({"qb_id": qb_id, "error": str(exc)[:200]})
    db.commit()
    deleted = _reconcile_entries_for(db, "Payment", seen_keys, start_date, end_date)
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}


async def pull_vendor_credits(
    tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "",
) -> dict[str, Any]:
    """Vendor credits — refunds/returns FROM a vendor that reduce a
    future BillPayment.

    Doesn't directly hit a bank account on its own — it's an A/P
    credit. Surfaced in the Banking feed as informational so the user
    can see why a future BillPayment netted less than the bill amount.

    Audit follow-up 2026-05-20: amount stored as 0 (not the credit
    value) so VendorCredit rows can NEVER contribute phantom cash to
    any future sum/roll-up over the unified feed. The credit amount is
    captured in the memo as "Credit: $X.XX" for display visibility.
    """
    where = _build_date_where(start_date, end_date)
    try:
        rows = await qb.query("VendorCredit", where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise

    seen_keys: set[tuple[str, int]] = set()
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    for raw in rows:
        qb_id = str(raw.get("Id") or "").strip()
        if not qb_id:
            continue
        seen_keys.add((qb_id, 0))
        try:
            vendor_ref = raw.get("VendorRef") or {}
            credit_amount = float(raw.get("TotalAmt") or 0)
            note = raw.get("PrivateNote") or ""
            memo = f"Credit: ${credit_amount:,.2f}" + (f" — {note}" if note else "")
            outcome = _upsert_banking_entry(
                db,
                qb_entity="VendorCredit",
                qb_txn_id=qb_id,
                qb_line_index=0,
                txn_date=_parse_date(raw.get("TxnDate")),
                amount=0,  # info-only — never affects any sum/roll-up.
                account_id=None,
                account_name=None,
                counterparty_name=vendor_ref.get("name") or None,
                memo=memo,
                raw_json=raw,
            )
            if outcome == "created":
                created += 1
            else:
                updated += 1
        except Exception as exc:
            log.exception("qb_pull_vendor_credits_row_failed qb_id=%s", qb_id)
            errors.append({"qb_id": qb_id, "error": str(exc)[:200]})
    db.commit()
    deleted = _reconcile_entries_for(db, "VendorCredit", seen_keys, start_date, end_date)
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}


async def pull_journal_entries(tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """JournalEntry expands to N rows — one per bank/CC-touching Line.

    Filters down to lines whose AccountRef points to an account we
    already know is a Bank or Credit Card (via qb_accounts).
    """
    where = _build_date_where(start_date, end_date)
    try:
        rows = await qb.query("JournalEntry", where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise

    # Cache bank/CC account IDs once for this pull. If qb_accounts is
    # missing or unpopulated, we have no way to distinguish banking-
    # affecting JE lines from non-banking ones — refuse to emit anything
    # rather than including everything. The Sync Banking endpoint runs
    # pull_accounts FIRST, so bank_ids should always be populated on
    # the unified-sync path.
    #
    # LOCs are deliberately NOT included here. A balanced LOC draw JE
    # posts TWO lines (DEBIT Bank + CREDIT LOC) — including the LOC
    # leg would emit a second cash-flow row and double the magnitude.
    # LOC activity already surfaces through Transfer (bank ↔ LOC) and
    # BillPayment-funded-from-LOC, so the user sees the cash event
    # exactly once. LOC-only JEs (interest accrual) aren't cash events
    # and shouldn't be in the feed — the balance card update on the
    # next pull_accounts reflects them.
    try:
        bank_ids = {
            r[0] for r in db.execute(text(
                "SELECT qb_account_id FROM qb_accounts WHERE account_type IN ('Bank', 'Credit Card')"
            )).all()
        }
    except (ProgrammingError, OperationalError):
        db.rollback()
        bank_ids = set()
    if not bank_ids:
        # Audit follow-up 2026-05-20: surface the upstream-missing state
        # as an explicit error in the response so the UI can warn (was a
        # silent zero, which masked accounts-pull failures during sync).
        log.warning(
            "qb_pull_journal_entries skipped — qb_accounts empty or missing for tenant %s; "
            "run pull_accounts first", tenant_id,
        )
        return {"created": 0, "updated": 0, "deleted": 0, "errors": [{
            "qb_id": "*",
            "error": "skipped: qb_accounts empty — run accounts sync first to identify bank accounts",
        }]}

    seen_keys: set[tuple[str, int]] = set()
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    for raw in rows:
        qb_id = str(raw.get("Id") or "").strip()
        if not qb_id:
            continue
        for line_idx, line in enumerate(raw.get("Line") or []):
            detail = (line or {}).get("JournalEntryLineDetail") or {}
            acct_ref = detail.get("AccountRef") or {}
            acct_id = str(acct_ref.get("value") or "")
            if not acct_id or acct_id not in bank_ids:
                continue  # Not a bank/CC line — skip.
            posting = str(detail.get("PostingType") or "").lower()
            sign = 1 if posting == "debit" else -1
            line_amount = float(line.get("Amount") or 0) * sign
            entity_ref = (detail.get("Entity") or {}).get("EntityRef") or {}
            seen_keys.add((qb_id, line_idx))
            try:
                outcome = _upsert_banking_entry(
                    db,
                    qb_entity="JournalEntry",
                    qb_txn_id=qb_id,
                    qb_line_index=line_idx,
                    txn_date=_parse_date(raw.get("TxnDate")),
                    amount=line_amount,
                    account_id=acct_id,
                    account_name=acct_ref.get("name") or None,
                    counterparty_name=entity_ref.get("name") or None,
                    memo=line.get("Description") or raw.get("PrivateNote") or None,
                    raw_json=line,
                )
                if outcome == "created":
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                log.exception("qb_pull_journal_entries_row_failed qb_id=%s line=%s", qb_id, line_idx)
                errors.append({"qb_id": f"{qb_id}:{line_idx}", "error": str(exc)[:200]})
    db.commit()
    deleted = _reconcile_entries_for(db, "JournalEntry", seen_keys, start_date, end_date)
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}


async def pull_transfers(
    tenant_id: str, db: Session, qb: QBClient, start_date: str = "", end_date: str = "",
) -> dict[str, Any]:
    where = _build_date_where(start_date, end_date)
    try:
        rows = await qb.query("Transfer", where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise

    seen_qb_ids: set[str] = set()
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    for raw in rows:
        qb_id = str(raw.get("Id") or "").strip()
        if not qb_id:
            continue
        seen_qb_ids.add(qb_id)
        try:
            f = raw.get("FromAccountRef") or {}
            t = raw.get("ToAccountRef") or {}
            existing = db.execute(
                text("SELECT id FROM qb_transfers WHERE qb_txn_id = :qid"),
                {"qid": qb_id},
            ).first()
            params = {
                "qid": qb_id,
                "td": _parse_date(raw.get("TxnDate")),
                "amt": float(raw.get("Amount") or 0),
                "fid": str(f.get("value") or "") or None,
                "fname": f.get("name") or None,
                "tid_": str(t.get("value") or "") or None,
                "tname": t.get("name") or None,
                "memo": raw.get("PrivateNote") or None,
                # See pull_deposits comment — psycopg2 needs JSON text.
                "raw": json.dumps(raw),
            }
            if existing:
                # Clear deleted_at — same inverse-operation logic as deposits.
                db.execute(text("""
                    UPDATE qb_transfers SET txn_date=:td, amount=:amt,
                        from_account_id=:fid, from_account_name=:fname,
                        to_account_id=:tid_, to_account_name=:tname,
                        memo=:memo, raw_json=:raw, last_synced_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP, deleted_at=NULL
                    WHERE qb_txn_id=:qid
                """), params)
                updated += 1
            else:
                params["id"] = str(uuid4())
                # Explicit timestamps — raw text() bypasses ORM default callables.
                db.execute(text("""
                    INSERT INTO qb_transfers (id, qb_txn_id, txn_date, amount,
                        from_account_id, from_account_name, to_account_id, to_account_name,
                        memo, raw_json, last_synced_at, created_at, updated_at)
                    VALUES (:id, :qid, :td, :amt, :fid, :fname, :tid_, :tname, :memo, :raw,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """), params)
                created += 1
        except Exception as exc:
            log.exception("qb_pull_transfers_row_failed qb_id=%s", qb_id)
            errors.append({"qb_id": qb_id, "error": str(exc)[:200]})
    db.commit()
    deleted = _reconcile_tombstones(db, "qb_transfers", seen_qb_ids, start_date, end_date)
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}


# ─────────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────────

# Account types from QBO that represent cash-side balances (asset accounts).
# These behave T-account-normal: debit increases, credit decreases.
_BANKING_ACCOUNT_TYPES = ("Bank", "Credit Card", "Other Current Asset")

# Liability AccountTypes that, when paired with AccountSubType=LineOfCredit,
# represent a borrowing facility. Behavior is INVERSE of bank accounts in
# T-account terms (credit increases the debt, debit pays it down) — so for
# cash-flow display we flip the JE sign on rows posted to LOC accounts.
_LOC_ACCOUNT_TYPES = ("Long Term Liability", "Other Current Liability")

# QBO emits the canonical "LineOfCredit" AccountSubType for the LOC pattern
# (verified developer.intuit.com Account API reference 2026-05-20). Don't
# add tolerant variants without a reproducer — prior-knowledge naming was
# the root cause of the 2026-05-20 CreditCardCredit / banking-sync outage.
_LOC_SUB_TYPES = ("LineOfCredit",)

# Canonical loan-style sub-types in QBO's AccountSubTypeEnum. LTL accounts
# are loans by definition regardless of sub-type, so the gate there is the
# account_type itself; OCL accounts only count as loans when carrying one
# of these specific sub-types (the rest are tax/payroll/clearing accounts).
_LOAN_SUB_TYPES_OCL = ("LoanPayable", "NotesPayable", "ShareholderNotesPayable")

# Liability accounts named like a loan still surface as loans even when
# the QBO sub-type is the generic OtherCurrentLiabilities — handles the
# 2026-05-20 GDX case where "Intuit Finance Loan" was mis-classified.
# Anchored on word boundaries so "Payroll Loan Officer Salary" doesn't
# false-positive (would need an unrelated entity to share the word).
import re as _loan_re
_LOAN_NAME_PATTERN = _loan_re.compile(r"\b(loan|notes?\s+payable|mortgage)\b", _loan_re.IGNORECASE)


def _liability_kind(account_type: str | None, account_sub_type: str | None, name: str | None = None) -> str | None:
    """Classify an account row.

    Returns one of:
      'cash' — Bank, Credit Card, Other Current Asset
      'loc'  — Line of Credit (LTL/OCL with AccountSubType=LineOfCredit)
      'loan' — Term loan / note / mortgage
                 LTL: any sub-type EXCEPT LineOfCredit
                 OCL: sub-type IN _LOAN_SUB_TYPES_OCL, OR name matches
                      loan-keyword regex (catches mis-classified rows)
       None  — anything else (tax payable, payroll clearing, etc.)
    """
    if account_type in _BANKING_ACCOUNT_TYPES:
        return "cash"
    sub = account_sub_type or ""
    if account_type in _LOC_ACCOUNT_TYPES and sub in _LOC_SUB_TYPES:
        return "loc"
    if account_type == "Long Term Liability":
        # All LTL is debt. LineOfCredit handled above; everything else
        # (NotesPayable, ShareholderNotesPayable, OtherLongTermLiabilities,
        # LongTermLiabilities catch-all) is a loan.
        return "loan"
    if account_type == "Other Current Liability":
        if sub in _LOAN_SUB_TYPES_OCL:
            return "loan"
        # Fallback: name-pattern match when sub-type is the generic catch-all.
        # Don't apply pattern match to sub-types that are clearly NOT loans
        # (PayrollTaxPayable, SalesTaxPayable, DirectDepositPayable, etc.) —
        # narrowed to the catch-all to avoid pulling in a misnamed tax row.
        if sub in ("OtherCurrentLiabilities", "") and name and _LOAN_NAME_PATTERN.search(name):
            return "loan"
    return None


def bank_balances(db: Session) -> list[dict[str, Any]]:
    """Read current balances for bank, credit-card, line-of-credit, and
    term-loan accounts from qb_accounts.

    Each row carries a `kind` discriminator ('cash', 'loc', or 'loan') so
    the UI can render each group with its own framing. Returns empty list
    if qb_accounts table doesn't exist yet (tenant hasn't run any QB sync).

    Filter strategy: SQL fetches the broad type set; _liability_kind
    classifies in Python. Tax/payroll/clearing sub-types fall through to
    None and get dropped, keeping the banking panel focused on
    cash + borrowing.
    """
    try:
        from sqlalchemy import bindparam
        # Both LTL and OCL get pulled and classified — LTL is always
        # loan-style; OCL is sub-type-gated. _liability_kind drops the
        # non-loan OCL rows (tax payable, payroll clearing, etc.).
        types = list(_BANKING_ACCOUNT_TYPES) + list(_LOC_ACCOUNT_TYPES)
        stmt = text(
            "SELECT qb_account_id, name, account_type, account_sub_type, current_balance, active "
            "FROM qb_accounts "
            "WHERE active = TRUE AND account_type IN :types "
            "ORDER BY account_type, name"
        ).bindparams(bindparam("types", expanding=True))
        rows = db.execute(stmt, {"types": types}).all()
    except (ProgrammingError, OperationalError):
        db.rollback()
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        kind = _liability_kind(r[2], r[3], r[1])
        if kind is None:
            continue
        out.append({
            "qb_account_id": r[0],
            "name": r[1],
            "account_type": r[2],
            "account_sub_type": r[3],
            "current_balance": float(r[4] or 0),
            "active": bool(r[5]),
            "kind": kind,
        })
    return out


def loc_account_ids(db: Session) -> set[str]:
    """Return the set of qb_account_id values for LineOfCredit accounts.

    Not consumed by any puller today — pull_journal_entries deliberately
    sticks to bank/CC ids to avoid double-counting balanced draw/paydown
    JEs (the bank leg already represents the cash event). This helper
    exists for a future LOC-activity surface (draws, paydowns, interest
    accrual) that will need to filter qb_banking_entries / Transfers by
    LOC account_id.
    """
    try:
        from sqlalchemy import bindparam
        stmt = text(
            "SELECT qb_account_id, account_sub_type FROM qb_accounts "
            "WHERE active = TRUE AND account_type IN :types"
        ).bindparams(bindparam("types", expanding=True))
        rows = db.execute(stmt, {"types": list(_LOC_ACCOUNT_TYPES)}).all()
    except (ProgrammingError, OperationalError):
        db.rollback()
        return set()
    return {r[0] for r in rows if (r[1] or "") in _LOC_SUB_TYPES}


def _iso(d: Any) -> str | None:
    """Best-effort YYYY-MM-DD string from a date / datetime / string column."""
    if d is None:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


# Kinds that map to qb_banking_entries.qb_entity verbatim. Lets the kind
# filter push down into the single source table instead of pulling all
# entries and filtering in Python.
_KIND_TO_BANKING_ENTRY_ENTITY = {
    "bill_payment": "BillPayment",
    "sales_receipt": "SalesReceipt",
    "refund_receipt": "RefundReceipt",
    "journal_entry": "JournalEntry",
    "customer_payment": "Payment",
    "vendor_credit": "VendorCredit",
}
_VALID_KINDS = ("purchase", "deposit", "transfer", *_KIND_TO_BANKING_ENTRY_ENTITY.keys())

# Map a row's discriminator to "in" / "out" / "transfer" so the UI can render
# a direction column without re-deriving from sign every render.
_KIND_TO_DIRECTION = {
    "purchase":         "out",   # vendor expense — always money OUT
    "deposit":          "in",    # bank deposit — money IN
    "transfer":         "transfer",  # both legs touch bank
    "bill_payment":     "out",
    "refund_receipt":   "out",   # customer refund — money OUT of bank
    "sales_receipt":    "in",
    "journal_entry":    "in",    # sign already on `amount`; direction set from sign at row-emit
    "customer_payment": "in",
    "vendor_credit":    "transfer",  # info-only (amount=0); not directional
}

# Sortable columns surface to the API. Whitelisted to keep raw user input
# out of the ORDER BY clause.
_SORT_KEYS = {"txn_date", "amount", "account", "counterparty", "kind", "txn_type"}


def _norm_kind_filter(kind: str | list[str] | None) -> list[str]:
    """Accept str/list/None. Returns a list of valid kinds, or [] for 'all'."""
    if not kind:
        return []
    if isinstance(kind, str):
        if kind == "all":
            return []
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
    else:
        kinds = [str(k).strip() for k in kind if str(k).strip()]
    return [k for k in kinds if k in _VALID_KINDS]


def _emit_purchase(r: Any) -> dict[str, Any]:
    """QBO Purchase.TotalAmt is non-negative; sign at emit so the unified
    feed presents money-OUT as a negative number. Direction is fixed 'out'."""
    raw_amt = float(r[5] or 0)
    signed = -abs(raw_amt) if raw_amt else 0.0
    return {
        "kind": "purchase",
        "direction": "out",
        "qb_txn_id": r[0],
        "txn_date": _iso(r[1]),
        "txn_type": r[2] or "Purchase",
        "account": r[3],
        "counterparty": r[4],
        "amount": signed,
        "memo": r[6],
        "linked_txn_ids": None,
    }


def _emit_deposit(r: Any) -> dict[str, Any]:
    return {
        "kind": "deposit",
        "direction": "in",
        "qb_txn_id": r[0],
        "txn_date": _iso(r[1]),
        "txn_type": "Deposit",
        "account": r[3],
        "counterparty": None,
        "amount": float(r[2] or 0),
        "memo": r[4],
        "linked_txn_ids": r[5],   # comma-separated "TxnType:TxnId" pairs
    }


def _emit_transfer(r: Any) -> dict[str, Any]:
    return {
        "kind": "transfer",
        "direction": "transfer",
        "qb_txn_id": r[0],
        "txn_date": _iso(r[1]),
        "txn_type": "Transfer",
        "account": r[3],
        "counterparty": r[4],
        "amount": float(r[2] or 0),
        "memo": r[5],
        "linked_txn_ids": None,
    }


def _emit_banking_entry(r: Any) -> dict[str, Any]:
    entity = r[0]
    kind = {
        "BillPayment": "bill_payment",
        "SalesReceipt": "sales_receipt",
        "RefundReceipt": "refund_receipt",
        "JournalEntry": "journal_entry",
        "Payment": "customer_payment",
        "VendorCredit": "vendor_credit",
    }.get(entity, "other")
    amount = float(r[3] or 0)
    # JournalEntry stores signed amounts (debit=+, credit=-). Other entities
    # use _KIND_TO_DIRECTION's static value. JE direction follows the sign.
    if kind == "journal_entry":
        direction = "in" if amount >= 0 else "out"
    else:
        direction = _KIND_TO_DIRECTION.get(kind, "transfer")
    return {
        "kind": kind,
        "direction": direction,
        "qb_txn_id": r[1],
        "txn_date": _iso(r[2]),
        "txn_type": entity,
        "account": r[4],
        "counterparty": r[5],
        "amount": amount,
        "memo": r[6],
        "linked_txn_ids": None,
    }


def _apply_search_in_python(rows: list[dict[str, Any]], search: str) -> list[dict[str, Any]]:
    """Case-insensitive substring match across account, counterparty, memo,
    qb_txn_id, txn_type. Run in Python rather than SQL so all 4 sources
    can share a single predicate without four parameterized ILIKE clauses."""
    if not search:
        return rows
    needle = search.strip().lower()
    if not needle:
        return rows
    def hit(row: dict[str, Any]) -> bool:
        for k in ("account", "counterparty", "memo", "qb_txn_id", "txn_type"):
            v = row.get(k)
            if v and needle in str(v).lower():
                return True
        return False
    return [r for r in rows if hit(r)]


def _sort_rows(rows: list[dict[str, Any]], order_by: str, order_dir: str) -> list[dict[str, Any]]:
    """Stable sort by the chosen column. Unknown columns fall back to
    txn_date desc. Sort keys coerce to strings for nulls-last semantics."""
    if order_by not in _SORT_KEYS:
        order_by = "txn_date"
    reverse = (order_dir or "desc").lower() != "asc"

    def key(row: dict[str, Any]) -> tuple[int, Any]:
        v = row.get(order_by)
        # Always put None at the END in the final order. Python sorts then
        # reverses, so for desc (reverse=True) None needs the SMALLEST flag
        # pre-reverse, and for asc (reverse=False) None needs the LARGEST
        # flag. Real values take the inverse.
        if v is None:
            return (0 if reverse else 1, "")
        flag = 1 if reverse else 0
        if order_by == "amount":
            return (flag, float(v))
        return (flag, str(v))

    return sorted(rows, key=key, reverse=reverse)


def unified_banking_transactions(
    db: Session,
    *,
    kind: str | list[str] | None = None,
    search: str = "",
    account: str = "",
    start_date: str = "",
    end_date: str = "",
    order_by: str = "txn_date",
    order_dir: str = "desc",
    page: int = 1,
    page_size: int = 25,
    paginated: bool = False,
    # Back-compat: callers using the old positional `limit` argument get a
    # bare list back. Existing tests pass `limit=` or nothing.
    limit: int | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Unified banking feed across 4 source tables with server-side filter,
    search, sort, and pagination.

    Return shape:
        paginated=True  → {items, total, page, page_size}  (route uses this)
        paginated=False → list[dict]                       (legacy back-compat)

    Filter pushdown: kind/start_date/end_date/account are pushed into each
    per-source SELECT so the 500-row cap is taken AFTER predicates apply —
    fixes the bug where filtering "Transfers" used to look empty because
    Purchases dominated the cap.
    """
    legacy_mode = not paginated
    effective_limit = limit if limit is not None else max(page_size * page * 4, 500)
    kinds = _norm_kind_filter(kind)

    # Per-source query helpers — each pushes the date/account filters into
    # the SQL and returns a bounded result. We pull up to `effective_limit`
    # per source to give the post-merge sort enough room to choose stable
    # winners across kinds before paginating.
    def _date_window_clause(prefix_col: str = "txn_date") -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if start_date:
            clauses.append(f"{prefix_col} >= :sd")
            params["sd"] = start_date
        if end_date:
            clauses.append(f"{prefix_col} <= :ed")
            params["ed"] = end_date
        return (" AND ".join(clauses), params)

    out: list[dict[str, Any]] = []
    want = lambda k: not kinds or k in kinds  # noqa: E731 — small inline filter helper

    # qb_bank_transactions — Purchases. Filter deleted_at IS NULL if the
    # column exists; older tenant DBs without it just see the table as-is.
    if want("purchase"):
        try:
            window, params = _date_window_clause("txn_date")
            where: list[str] = []
            # The deleted_at column was added in 2026-05-20 sprint. Try the
            # strict form first; fall back to the unfiltered form if the
            # column doesn't exist (savepoint via try/except).
            if window:
                where.append(window)
            try:
                strict = "SELECT qb_txn_id, txn_date, txn_type, account_name, payee, amount, memo FROM qb_bank_transactions WHERE deleted_at IS NULL"
                if where:
                    strict += " AND " + " AND ".join(where)
                strict += " ORDER BY txn_date DESC NULLS LAST LIMIT :lim"
                rows_p = db.execute(text(strict), {**params, "lim": effective_limit}).all()
            except (ProgrammingError, OperationalError):
                db.rollback()
                fb = "SELECT qb_txn_id, txn_date, txn_type, account_name, payee, amount, memo FROM qb_bank_transactions"
                if where:
                    fb += " WHERE " + " AND ".join(where)
                fb += " ORDER BY txn_date DESC NULLS LAST LIMIT :lim"
                rows_p = db.execute(text(fb), {**params, "lim": effective_limit}).all()
            for r in rows_p:
                if account and (r[3] or "") != account:
                    continue
                out.append(_emit_purchase(r))
        except (ProgrammingError, OperationalError):
            db.rollback()

    if want("deposit"):
        try:
            window, params = _date_window_clause("txn_date")
            base = (
                "SELECT qb_txn_id, txn_date, total_amount, deposit_to_account_name, "
                "memo, linked_qb_ids FROM qb_deposits WHERE deleted_at IS NULL"
            )
            try:
                sql = base
                if window:
                    sql += " AND " + window
                sql += " ORDER BY txn_date DESC NULLS LAST LIMIT :lim"
                rows_d = db.execute(text(sql), {**params, "lim": effective_limit}).all()
            except (ProgrammingError, OperationalError):
                db.rollback()
                # Legacy tenant without linked_qb_ids column — degrade
                # gracefully by re-selecting without it (None for that slot).
                fb = (
                    "SELECT qb_txn_id, txn_date, total_amount, deposit_to_account_name, "
                    "memo, NULL FROM qb_deposits WHERE deleted_at IS NULL"
                )
                if window:
                    fb += " AND " + window
                fb += " ORDER BY txn_date DESC NULLS LAST LIMIT :lim"
                rows_d = db.execute(text(fb), {**params, "lim": effective_limit}).all()
            for r in rows_d:
                if account and (r[3] or "") != account:
                    continue
                out.append(_emit_deposit(r))
        except (ProgrammingError, OperationalError):
            db.rollback()

    if want("transfer"):
        try:
            window, params = _date_window_clause("txn_date")
            sql = (
                "SELECT qb_txn_id, txn_date, amount, from_account_name, "
                "to_account_name, memo FROM qb_transfers WHERE deleted_at IS NULL"
            )
            if window:
                sql += " AND " + window
            sql += " ORDER BY txn_date DESC NULLS LAST LIMIT :lim"
            for r in db.execute(text(sql), {**params, "lim": effective_limit}).all():
                if account and (r[3] or "") != account and (r[4] or "") != account:
                    continue
                out.append(_emit_transfer(r))
        except (ProgrammingError, OperationalError):
            db.rollback()

    # qb_banking_entries — push the qb_entity predicate down when only a
    # subset of the 6 entity kinds was requested.
    entries_kinds = [k for k in kinds if k in _KIND_TO_BANKING_ENTRY_ENTITY] if kinds else None
    if (not kinds) or entries_kinds:
        try:
            window, params = _date_window_clause("txn_date")
            base = (
                "SELECT qb_entity, qb_txn_id, txn_date, amount, account_name, "
                "counterparty_name, memo FROM qb_banking_entries "
                "WHERE deleted_at IS NULL"
            )
            if entries_kinds:
                entities = [_KIND_TO_BANKING_ENTRY_ENTITY[k] for k in entries_kinds]
                from sqlalchemy import bindparam
                base += " AND qb_entity IN :ents"
                params["ents"] = entities
            if window:
                base += " AND " + window
            base += " ORDER BY txn_date DESC NULLS LAST LIMIT :lim"
            stmt = text(base)
            if entries_kinds:
                from sqlalchemy import bindparam
                stmt = stmt.bindparams(bindparam("ents", expanding=True))
            for r in db.execute(stmt, {**params, "lim": effective_limit}).all():
                if account and (r[4] or "") != account:
                    continue
                out.append(_emit_banking_entry(r))
        except (ProgrammingError, OperationalError):
            db.rollback()

    # Search runs in Python so all 4 sources share one predicate without
    # parallel ILIKE clauses. Acceptable up to effective_limit*4 rows.
    out = _apply_search_in_python(out, search)
    out = _sort_rows(out, order_by, order_dir)

    if legacy_mode:
        return out[: (limit or 500)]

    total = len(out)
    start = max(0, (max(1, page) - 1) * max(1, page_size))
    end = start + max(1, page_size)
    return {
        "items": out[start:end],
        "total": total,
        "page": max(1, page),
        "page_size": max(1, page_size),
    }


# ─────────────────────────────────────────────────────────────────────
# Schedule helpers
# ─────────────────────────────────────────────────────────────────────


def _frequency_delta(frequency: str) -> timedelta | None:
    return {
        FREQ_HOURLY: timedelta(hours=1),
        FREQ_EVERY_4H: timedelta(hours=4),
        FREQ_DAILY: timedelta(days=1),
        FREQ_WEEKLY: timedelta(days=7),
    }.get(frequency)


def compute_next_run_at(frequency: str, base: datetime | None = None) -> datetime | None:
    """Manual → None (never scheduled). Other frequencies → base + delta."""
    delta = _frequency_delta(frequency)
    if delta is None:
        return None
    return (base or _utcnow()) + delta


def get_or_create_schedule(db: Session) -> QBSyncSchedule:
    """Returns the schedule row, creating one if absent.

    Audit follow-up 2026-05-20: the singleton invariant is enforced by
    "first row wins" rather than a UNIQUE constraint (the table has no
    natural constant column to constrain on). A race that inserts two
    rows is recovered by picking the oldest and ignoring the rest —
    `.first()` instead of `.scalar_one_or_none()` so the previously-
    raised MultipleResultsFound is replaced by deterministic recovery.
    """
    from sqlalchemy import select
    row = db.execute(
        select(QBSyncSchedule).order_by(QBSyncSchedule.created_at.asc())
    ).scalars().first()
    if row is None:
        row = QBSyncSchedule(frequency=FREQ_MANUAL)
        db.add(row)
        try:
            db.commit()
        except Exception:
            # Concurrent insert race — fall back to whoever won.
            db.rollback()
            row = db.execute(
                select(QBSyncSchedule).order_by(QBSyncSchedule.created_at.asc())
            ).scalars().first()
            if row is None:
                raise
        db.refresh(row)
    return row


def update_schedule(db: Session, frequency: str) -> QBSyncSchedule:
    if frequency not in VALID_FREQUENCIES:
        raise ValueError(f"invalid frequency: {frequency}")
    row = get_or_create_schedule(db)
    row.frequency = frequency
    row.next_run_at = compute_next_run_at(frequency)
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


def schedule_dict(s: QBSyncSchedule) -> dict[str, Any]:
    return {
        "frequency": s.frequency,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "last_run_status": s.last_run_status,
        "last_run_error": s.last_run_error,
    }


def record_scheduled_run(db: Session, status: str, error: str | None = None) -> None:
    """Called by the dispatcher after running a scheduled sync. Sets
    last_run_at = now and rolls next_run_at forward by the frequency delta."""
    s = get_or_create_schedule(db)
    now = _utcnow()
    s.last_run_at = now
    s.last_run_status = status
    s.last_run_error = (error or "")[:500] if error else None
    s.next_run_at = compute_next_run_at(s.frequency, base=now)
    db.commit()


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


import re as _re

_DATE_PATTERN = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: str, label: str) -> str:
    """Strictly accept YYYY-MM-DD before interpolating into a QBO query.
    QBO's query string is string-only (no bound params); a malformed value
    would either inject (commas, single quotes) or fail mid-query.
    """
    if not value:
        return ""
    if not _DATE_PATTERN.match(value):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return value


def _reconcile_tombstones(
    db: Session, table: str, seen_qb_ids: set[str], start_date: str, end_date: str,
) -> int:
    """Mark rows in `table` as deleted_at = now() when they were previously
    synced but no longer appear in the QBO response.

    Scoped to the synced date window: if start_date / end_date are set,
    we only reconcile rows whose txn_date is within that window. Without
    a window, we reconcile every active (deleted_at IS NULL) row in the
    table — the safest interpretation of "user asked for a full sync."

    Returns the count of rows newly tombstoned.
    """
    if table not in {"qb_deposits", "qb_transfers"}:
        raise ValueError(f"unexpected table for reconcile: {table}")
    params: dict[str, Any] = {}
    where = ["deleted_at IS NULL"]
    if seen_qb_ids:
        # Expanding bindparam — SQLAlchemy compiles to ('a', 'b', ...).
        from sqlalchemy import bindparam
        where.append("qb_txn_id NOT IN :seen")
        params["seen"] = list(seen_qb_ids)
    if start_date:
        where.append("(txn_date >= :sd OR txn_date IS NULL)")
        params["sd"] = start_date
    if end_date:
        where.append("(txn_date <= :ed OR txn_date IS NULL)")
        params["ed"] = end_date

    sql = f"UPDATE {table} SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE " + " AND ".join(where)
    stmt = text(sql)
    if "seen" in params:
        from sqlalchemy import bindparam
        stmt = stmt.bindparams(bindparam("seen", expanding=True))
    result = db.execute(stmt, params)
    db.commit()
    return int(result.rowcount or 0)


def _build_date_where(start_date: str, end_date: str) -> str:
    parts: list[str] = []
    s = _validate_date(start_date, "start_date")
    e = _validate_date(end_date, "end_date")
    if s:
        parts.append(f"TxnDate >= '{s}'")
    if e:
        parts.append(f"TxnDate <= '{e}'")
    return " AND ".join(parts)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
