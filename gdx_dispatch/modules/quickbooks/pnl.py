"""QuickBooks Online ProfitAndLoss report sync.

One API call per (tenant, year) returns all 12 months × every P&L account
when ``summarize_column_by=Month`` is set. Cached into ``qb_pnl_monthly``
so the budget UI can render variance fast without re-hitting QBO.

Why a dedicated module: the Reports endpoint shape (Header/Columns/Rows
tree with nested Section/Data rows) is fundamentally different from the
entity endpoints (Customer/Invoice/Purchase) the rest of qbmodules
handles. Folding it into ``sync.py`` would couple two unrelated parsers.

Sprint monthly-budget (2026-05-24).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.modules.quickbooks.client import QBClient


log = logging.getLogger(__name__)


# Account types the QBO ProfitAndLoss report surfaces. Stored as
# `account_type` on qb_pnl_monthly so callers can filter (expenses vs
# income vs COGS) without joining qb_accounts.
ACCOUNT_TYPE_EXPENSE = "Expense"
ACCOUNT_TYPE_COGS = "Cost of Goods Sold"
ACCOUNT_TYPE_OTHER_EXPENSE = "Other Expense"
ACCOUNT_TYPE_INCOME = "Income"
ACCOUNT_TYPE_OTHER_INCOME = "Other Income"

# QBO P&L Section.group values → our normalized account_type.
# group is set on Section rows; we propagate it down to Data leaves.
GROUP_TO_ACCOUNT_TYPE: dict[str, str] = {
    "Income": ACCOUNT_TYPE_INCOME,
    "OtherIncome": ACCOUNT_TYPE_OTHER_INCOME,
    "COGS": ACCOUNT_TYPE_COGS,
    "Expenses": ACCOUNT_TYPE_EXPENSE,
    "OtherExpenses": ACCOUNT_TYPE_OTHER_EXPENSE,
}

EXPENSE_TYPES: frozenset[str] = frozenset({
    ACCOUNT_TYPE_EXPENSE,
    ACCOUNT_TYPE_COGS,
    ACCOUNT_TYPE_OTHER_EXPENSE,
})


def _parse_amount(s: str | None) -> Decimal:
    if s is None or s == "":
        return Decimal("0")
    try:
        # QBO uses plain "1234.56" — no commas, no currency symbol.
        return Decimal(str(s))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _month_index_from_col(col: dict[str, Any]) -> int | None:
    """Given a Columns.Column entry, return the 1-12 month index or None.

    Column metadata for a month column with summarize_column_by=Month has
    a MetaData entry like {Name: 'StartDate', Value: '2026-01-01'}.
    We parse the StartDate's month rather than ColTitle to avoid locale
    surprises ('Jan 2026' vs 'January 2026' vs '2026-01').
    """
    meta = col.get("MetaData") or []
    for entry in meta:
        if entry.get("Name") == "StartDate":
            try:
                d = date.fromisoformat(str(entry.get("Value") or "")[:10])
                return d.month
            except ValueError:
                return None
    return None


def _walk_rows(
    rows: list[dict[str, Any]],
    *,
    inherited_type: str | None,
    month_columns: dict[int, int],  # column_index → month (1..12)
    out: dict[tuple[int, str], dict[str, Any]],
) -> None:
    """Walk Rows.Row[] tree, collecting leaf Data rows.

    QBO P&L structure (verified by observation across multiple tenants):

      Rows.Row = [
        { type: "Section",
          group: "Income" | "Expenses" | "COGS" | "OtherIncome" | "OtherExpenses",
          Header: { ColData: [...] },
          Rows: { Row: [ <nested rows> ] },
          Summary: { ColData: [...] } }
      ]

      Data leaf:
      { type: "Data",
        ColData: [
          { value: "Service Income", id: "1" },   # account name + id
          { value: "1234.56" },                    # Jan amount
          { value: "987.65" },                     # Feb amount
          ...
        ] }

    `id` on ColData[0] is the QBO Account ID — present when the row is a
    real account (not a calculated subtotal). We drop rows without an id.

    Mutates `out` in place: out[(month, account_id)] = {amount, name, type}.
    """
    for row in rows or []:
        row_type = row.get("type")
        group = row.get("group")
        # group may be empty string on nested sections; only override when set
        section_type = GROUP_TO_ACCOUNT_TYPE.get(group, inherited_type) if group else inherited_type

        if row_type == "Section":
            nested = (row.get("Rows") or {}).get("Row") or []
            _walk_rows(nested, inherited_type=section_type, month_columns=month_columns, out=out)
            continue

        if row_type == "Data":
            col_data = row.get("ColData") or []
            if not col_data:
                continue
            first = col_data[0] if isinstance(col_data[0], dict) else {}
            account_id = str(first.get("id") or "").strip()
            account_name = str(first.get("value") or "").strip()
            if not account_id:
                # Calculated row (e.g. "Total Income") — skip; we sum from leaves.
                continue
            for col_idx, month in month_columns.items():
                if col_idx >= len(col_data):
                    continue
                cell = col_data[col_idx]
                if not isinstance(cell, dict):
                    continue
                amount = _parse_amount(cell.get("value"))
                if amount == 0:
                    # Skip zero cells to keep the cache lean; absence == zero.
                    continue
                key = (month, account_id)
                out[key] = {
                    "amount": amount,
                    "account_name": account_name,
                    "account_type": section_type,
                }


def parse_profit_and_loss(report: dict[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
    """Parse a QBO ProfitAndLoss report JSON into {(month, account_id): row}.

    Raises ValueError if the report doesn't look like a Monthly-summarized
    P&L (so callers fail loudly rather than silently caching empty data).
    """
    columns = (report.get("Columns") or {}).get("Column") or []
    if not columns:
        raise ValueError("ProfitAndLoss response missing Columns")

    # First column is the account label; remaining are months + a Total.
    # We only want the month columns.
    month_columns: dict[int, int] = {}
    for idx, col in enumerate(columns):
        if idx == 0:
            continue  # account label column
        month = _month_index_from_col(col)
        if month is not None:
            month_columns[idx] = month

    if not month_columns:
        raise ValueError(
            "ProfitAndLoss response has no month columns — did you set "
            "summarize_column_by=Month?"
        )

    rows = (report.get("Rows") or {}).get("Row") or []
    out: dict[tuple[int, str], dict[str, Any]] = {}
    _walk_rows(rows, inherited_type=None, month_columns=month_columns, out=out)
    return out


async def fetch_profit_and_loss(
    qb: QBClient,
    *,
    year: int,
    accounting_method: str = "Accrual",
) -> dict[str, Any]:
    """Fetch the QBO ProfitAndLoss report for a year, summarized by month.

    Returns the raw report JSON. Use parse_profit_and_loss() to walk it.
    """
    start = f"{year:04d}-01-01"
    end = f"{year:04d}-12-31"
    # Use the existing httpx client on QBClient; it carries auth + base_url.
    url = (
        f"/v3/company/{qb.realm_id}/reports/ProfitAndLoss"
        f"?start_date={start}&end_date={end}"
        f"&summarize_column_by=Month&accounting_method={accounting_method}"
        f"&minorversion={qb.minor_version}"
    )
    resp = await qb._client.get(url)  # noqa: SLF001 — intentional reuse
    qb._raise_for_status(resp)  # noqa: SLF001
    return resp.json()


def upsert_pnl_rows(
    db: Session,
    *,
    year: int,
    parsed: dict[tuple[int, str], dict[str, Any]],
) -> dict[str, int]:
    """Refresh one year of qb_pnl_monthly atomically.

    DELETE-then-INSERT inside one transaction with explicit commit — a
    mid-INSERT failure rolls back the DELETE so the year either fully
    refreshes or stays as it was. The 2026-05-24 walk-on-prod incident
    proved why this needs an explicit commit: a prior version used
    ``with db.begin_nested():`` and the savepoint release did NOT commit
    the outer FastAPI session, so 200 OK responses landed with zero
    persisted rows. Fixed by committing explicitly at the end.

    Strategy is wipe+reinsert (not row-level upsert) because the QBO
    report is the source of truth — partial-keep semantics would leave
    stale rows for accounts that no longer post to that month.

    Returns {deleted, inserted} counts.
    """
    now = datetime.now(timezone.utc)
    try:
        deleted = db.execute(
            text("DELETE FROM qb_pnl_monthly WHERE year = :y"),
            {"y": year},
        ).rowcount or 0

        inserted = 0
        for (month, account_id), row in parsed.items():
            db.execute(
                text(
                    "INSERT INTO qb_pnl_monthly "
                    "(id, year, month, qb_account_id, account_name, account_type, amount, synced_at) "
                    "VALUES (:id, :y, :m, :aid, :aname, :atype, :amt, :ts)"
                ),
                {
                    "id": str(uuid4()),
                    "y": year,
                    "m": month,
                    "aid": account_id,
                    "aname": row.get("account_name"),
                    "atype": row.get("account_type"),
                    # Coerce Decimal → str at bind time: psycopg2 (Postgres)
                    # accepts Decimal natively but pysqlite (tests) does
                    # not. Both backends parse a numeric string into the
                    # NUMERIC column correctly. The walking-prod test
                    # 2026-05-24 caught this divergence.
                    "amt": str(row.get("amount") or Decimal("0")),
                    "ts": now,
                },
            )
            inserted += 1
        db.commit()
    except Exception:
        # Any failure (parse, INSERT, constraint violation) rolls back
        # the DELETE too — atomic refresh.
        db.rollback()
        raise
    return {"deleted": int(deleted), "inserted": inserted}


async def fetch_profit_and_loss_detail(
    qb: QBClient,
    *,
    start_date: str,
    end_date: str,
    account_id: str | None = None,
    accounting_method: str = "Accrual",
) -> dict[str, Any]:
    """Fetch the QBO ProfitAndLossDetail report for a date range.

    When account_id is supplied, QBO filters the report to that single
    account — exactly what the "Fix in QuickBooks" panel needs to surface
    miscategorized transactions on an anomalous account.
    """
    url = (
        f"/v3/company/{qb.realm_id}/reports/ProfitAndLossDetail"
        f"?start_date={start_date}&end_date={end_date}"
        f"&accounting_method={accounting_method}"
        f"&minorversion={qb.minor_version}"
    )
    if account_id:
        url += f"&account={account_id}"
    resp = await qb._client.get(url)  # noqa: SLF001
    qb._raise_for_status(resp)  # noqa: SLF001
    return resp.json()


def parse_profit_and_loss_detail(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a ProfitAndLossDetail tree and return a flat list of transactions.

    Each leaf Data row in the report represents one posting against the
    account. ColData column order (verified against GDX live response 2026-05-25):

        [0] Date              {"value": "2026-01-06"}
        [1] Transaction Type  {"value": "Expense", "id": "2636"}   ← txn id lives HERE
        [2] Num               {"value": ""}
        [3] Name (Vendor)     {"value": "North Star Gas", "id": "195"}
        [4] Memo/Description  {"value": "..."}
        [5] Split (bank acct) {"value": "Garage Door inc Main", "id": "178"}
        [6] Amount            {"value": "77.11"}
        [7] Balance           {"value": "150.08"}

    Importantly, the transaction id is on ColData[1] (Transaction Type), NOT
    on ColData[0] (Date). The walk-the-prod 2026-05-25 walk caught this —
    the unit-test fixture had it on cd[0] because I guessed wrong about the
    shape. Subtotal rows have no `id` on cd[1] so they're correctly skipped.

    Returns each row as:
        {txn_id, txn_type, txn_date, vendor_name, vendor_id, memo, split, amount}
    """
    out: list[dict[str, Any]] = []

    def walk(rs):
        for r in rs:
            t = r.get("type")
            if t == "Section":
                inner = (r.get("Rows") or {}).get("Row") or []
                walk(inner)
            elif t == "Data":
                cd = r.get("ColData") or []
                if len(cd) < 7:
                    continue
                type_cell = cd[1] if isinstance(cd[1], dict) else {}
                txn_id = str(type_cell.get("id") or "").strip()
                if not txn_id:
                    # Subtotal / summary row (e.g. "Total for Vehicle gas & fuel")
                    continue
                txn_type = str(type_cell.get("value") or "").strip()
                txn_date = str((cd[0] or {}).get("value") or "").strip()
                vendor_cell = cd[3] if isinstance(cd[3], dict) else {}
                vendor = str(vendor_cell.get("value") or "").strip()
                vendor_id = str(vendor_cell.get("id") or "").strip() or None
                memo = str((cd[4] or {}).get("value") or "").strip()
                split = str((cd[5] or {}).get("value") or "").strip()
                amt_str = str((cd[6] or {}).get("value") or "0").strip()
                try:
                    amt = Decimal(amt_str)
                except InvalidOperation:
                    amt = Decimal("0")
                out.append({
                    "txn_id": txn_id,
                    "txn_type": txn_type,
                    "txn_date": txn_date,
                    "vendor_name": vendor,
                    "vendor_id": vendor_id,
                    "memo": memo,
                    "split": split,
                    "amount": amt,
                })
    rows = (report.get("Rows") or {}).get("Row") or []
    walk(rows)
    return out


async def pull_profit_and_loss(
    tenant_id: str,
    db: Session,
    qb: QBClient,
    *,
    year: int,
    accounting_method: str = "Accrual",
) -> dict[str, Any]:
    """End-to-end: fetch one year of P&L from QBO and cache into qb_pnl_monthly.

    Returns {year, deleted, inserted, account_count, accounting_method}.
    """
    report = await fetch_profit_and_loss(qb, year=year, accounting_method=accounting_method)
    parsed = parse_profit_and_loss(report)
    counts = upsert_pnl_rows(db, year=year, parsed=parsed)
    account_ids = {aid for (_m, aid) in parsed.keys()}
    log.info(
        "qb_pnl_pulled tenant=%s year=%d basis=%s accounts=%d rows=%d",
        tenant_id, year, accounting_method, len(account_ids), counts["inserted"],
    )
    return {
        "year": year,
        "deleted": counts["deleted"],
        "inserted": counts["inserted"],
        "account_count": len(account_ids),
        "accounting_method": accounting_method,
    }
