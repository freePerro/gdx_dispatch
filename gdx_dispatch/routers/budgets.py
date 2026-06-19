"""Monthly budget router.

Sprint monthly-budget (2026-05-24). One budget per tenant. Actuals come
from the qb_pnl_monthly cache (filled by gdx_dispatch.modules.quickbooks.pnl).
Budget lives in GDX; QB is read-only for budgets.

Role model:
- Read: ``accounting.read`` (admin, owner, accounting role)
- Write: ``accounting.write``
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.models.tenant_models import AppSettings, MonthlyBudget
from gdx_dispatch.modules.forecasting.service import revenue_projection
from gdx_dispatch.modules.quickbooks.oauth import get_qb_client
from gdx_dispatch.modules.quickbooks.pnl import (
    EXPENSE_TYPES,
    fetch_profit_and_loss_detail,
    parse_profit_and_loss_detail,
    pull_profit_and_loss,
)
from gdx_dispatch.modules.quickbooks.recategorize import (
    RecategorizeError,
    SUPPORTED_TYPES,
    recategorize_transaction,
    suggest_target_account,
)


log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


# ---------- pydantic schemas ----------

LINE_TYPES = frozenset({"fixed", "variable", "percent_of_revenue"})


class BudgetLineIn(BaseModel):
    """Create a new budget line."""
    year: int = Field(..., ge=2000, le=2999)
    month: int = Field(..., ge=1, le=12)
    qb_account_id: str = Field(..., min_length=1, max_length=64)
    account_name: str | None = Field(default=None, max_length=300)
    amount: Decimal = Field(default=Decimal("0"))
    line_type: str = Field(default="fixed")
    pct_of_revenue: Decimal | None = Field(default=None, ge=0, le=1)
    notes: str | None = None


class BudgetLineUpdate(BaseModel):
    """Edit an existing budget line — all fields optional."""
    amount: Decimal | None = None
    line_type: str | None = None
    pct_of_revenue: Decimal | None = Field(default=None, ge=0, le=1)
    notes: str | None = None
    is_locked: bool | None = None


class BudgetLineOut(BaseModel):
    id: str
    year: int
    month: int
    qb_account_id: str
    account_name: str | None
    account_type: str | None
    amount: Decimal
    line_type: str
    pct_of_revenue: Decimal | None
    source: str
    is_locked: bool
    notes: str | None
    actual: Decimal
    variance: Decimal
    variance_pct: float | None
    # Decision context — surfaced so users budget WITH history in front of
    # them, not by clicking a button that overwrites their inputs.
    trailing_3mo_avg: Decimal
    trailing_6mo_avg: Decimal
    same_month_last_year: Decimal | None


# ---------- helpers ----------


def _snap_to_ten(amount: Decimal) -> Decimal:
    """Round to nearest $10 — used by auto-seed only."""
    if amount <= 0:
        return Decimal("0")
    return (amount / Decimal("10")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("10")


def _validate_line_type(lt: str) -> None:
    if lt not in LINE_TYPES:
        raise HTTPException(400, f"line_type must be one of {sorted(LINE_TYPES)}")


def _tenant_id(request: Request) -> str:
    t = getattr(request.state, "tenant", None) or {}
    tid = t.get("id") if isinstance(t, dict) else None
    if not tid:
        raise HTTPException(400, "tenant context not resolved")
    return str(tid)


def _actuals_for_month(
    db: Session, *, year: int, month: int,
) -> dict[str, dict[str, Any]]:
    """Return {qb_account_id: {amount, account_name, account_type}} for one month.

    Pulls from qb_pnl_monthly cache. Empty dict if month hasn't been synced.
    """
    rows = db.execute(
        text(
            "SELECT qb_account_id, account_name, account_type, amount "
            "FROM qb_pnl_monthly WHERE year = :y AND month = :m"
        ),
        {"y": year, "m": month},
    ).all()
    return {
        r.qb_account_id: {
            "amount": Decimal(r.amount or 0),
            "account_name": r.account_name,
            "account_type": r.account_type,
        }
        for r in rows
    }


def _expense_accounts_from_pnl(db: Session) -> list[dict[str, Any]]:
    """Distinct expense/COGS accounts that have appeared in any cached P&L month."""
    rows = db.execute(
        text(
            "SELECT qb_account_id, "
            "MAX(account_name) AS account_name, "
            "MAX(account_type) AS account_type "
            "FROM qb_pnl_monthly "
            "WHERE account_type IN ('Expense','Cost of Goods Sold','Other Expense') "
            "GROUP BY qb_account_id "
            "ORDER BY MAX(account_type), MAX(account_name)"
        )
    ).all()
    return [
        {
            "qb_account_id": r.qb_account_id,
            "account_name": r.account_name,
            "account_type": r.account_type,
        }
        for r in rows
    ]


def _revenue_basis_for_month(db: Session, *, year: int, month: int) -> Decimal:
    """Revenue basis for the (year, month) being viewed — used by
    percent_of_revenue lines.

    Past or current-completed months → SUM(Income rows) from qb_pnl_monthly
    for that month (actuals from QBO). Current or future months → live
    revenue_projection() over the next 30 days as a forward proxy.

    The auditor 2026-05-24 caught the prior implementation always returned
    "next 30 days from today" regardless of month, which made
    percent_of_revenue lies on every historical month. Fixed.
    """
    today = date.today()
    is_future = (year, month) > (today.year, today.month)
    is_current = (year, month) == (today.year, today.month)

    if not is_future and not is_current:
        # Past month — use actual income from cached P&L.
        row = db.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) AS total "
                "FROM qb_pnl_monthly "
                "WHERE year = :y AND month = :m "
                "AND account_type IN ('Income', 'Other Income')"
            ),
            {"y": year, "m": month},
        ).one()
        return Decimal(row.total or 0)

    # Current or future month — forecast.
    try:
        proj = revenue_projection(db, window_days=30)
        return Decimal(str(proj.get("expected_total") or 0))
    except Exception:  # noqa: BLE001
        log.exception("revenue_projection_failed_in_budgets")
        return Decimal("0")


def _trailing_window(year: int, month: int, n_months: int) -> list[tuple[int, int]]:
    """Return the previous N (year, month) tuples ending at (year, month-1)."""
    out: list[tuple[int, int]] = []
    y, m = year, month
    for _ in range(n_months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append((y, m))
    return out


def _history_for_accounts(
    db: Session, *, year: int, month: int, account_ids: list[str],
) -> dict[str, dict[str, Decimal | None]]:
    """One batched query → {account_id: {trailing_3mo_avg, trailing_6mo_avg, same_month_last_year}}.

    Six-month lookback for the avg windows, plus same-month-prior-year.
    All amounts come from qb_pnl_monthly (the cached QBO P&L).
    """
    if not account_ids:
        return {}
    # Composite-key encoding so the IN works on both Postgres and sqlite.
    window_6 = _trailing_window(year, month, 6)
    prior_year_same_month_key = (year - 1) * 100 + month
    lookback_keys = [y * 100 + m for (y, m) in window_6] + [prior_year_same_month_key]
    stmt = text(
        "SELECT qb_account_id, year, month, amount FROM qb_pnl_monthly "
        "WHERE qb_account_id IN :ids "
        "AND (year * 100 + month) IN :keys"
    ).bindparams(
        bindparam("ids", expanding=True),
        bindparam("keys", expanding=True),
    )
    rows = db.execute(stmt, {"ids": account_ids, "keys": lookback_keys}).all()

    # Bucket: {account_id: {(year, month): amount}}
    buckets: dict[str, dict[tuple[int, int], Decimal]] = {}
    for r in rows:
        buckets.setdefault(r.qb_account_id, {})[(r.year, r.month)] = Decimal(r.amount or 0)

    keys_3 = set(_trailing_window(year, month, 3))
    keys_6 = set(window_6)
    prior_key = (year - 1, month)

    out: dict[str, dict[str, Decimal | None]] = {}
    for aid in account_ids:
        b = buckets.get(aid, {})
        # IMPORTANT: average over months we actually have data for, NOT
        # the fixed window size. qb_pnl_monthly is SPARSE — rows only
        # exist for months with activity. Auditor 2026-05-24 caught the
        # fixed-denominator bug: a tenant with 2 months of P&L would see
        # 6-mo avg = sum/6 (3x understated), and quick-fill writes that
        # lie straight into the budget. SpendingTrendsView already gets
        # this right; the backend now matches.
        amounts_3 = [amt for (k, amt) in b.items() if k in keys_3]
        amounts_6 = [amt for (k, amt) in b.items() if k in keys_6]
        avg_3 = (sum(amounts_3, Decimal("0")) / Decimal(len(amounts_3))).quantize(Decimal("0.01")) \
            if amounts_3 else Decimal("0")
        avg_6 = (sum(amounts_6, Decimal("0")) / Decimal(len(amounts_6))).quantize(Decimal("0.01")) \
            if amounts_6 else Decimal("0")
        last_year_val = b.get(prior_key)
        out[aid] = {
            "trailing_3mo_avg": avg_3,
            "trailing_6mo_avg": avg_6,
            "months_with_data_3mo": len(amounts_3),
            "months_with_data_6mo": len(amounts_6),
            "same_month_last_year": last_year_val,
        }
    return out


def _pnl_last_synced_at(db: Session) -> datetime | None:
    """MAX(synced_at) across qb_pnl_monthly — for the freshness indicator."""
    try:
        row = db.execute(
            text("SELECT MAX(synced_at) AS ts FROM qb_pnl_monthly")
        ).one()
        return row.ts
    except Exception:  # noqa: BLE001
        log.exception("pnl_last_synced_at_failed")
        return None


def _resolve_budget_amount(
    line: MonthlyBudget, monthly_revenue: Decimal,
) -> Decimal:
    """Compute the effective dollar amount for a line, applying line_type."""
    if line.line_type == "percent_of_revenue" and line.pct_of_revenue is not None:
        return (Decimal(line.pct_of_revenue) * monthly_revenue).quantize(Decimal("0.01"))
    return Decimal(line.amount or 0)


def _serialize(
    line: MonthlyBudget,
    actual: Decimal,
    *,
    monthly_revenue: Decimal,
    account_meta: dict[str, Any] | None,
    history: dict[str, Decimal | None] | None = None,
) -> BudgetLineOut:
    effective = _resolve_budget_amount(line, monthly_revenue)
    variance = actual - effective
    variance_pct: float | None = None
    if effective and effective != 0:
        variance_pct = float((variance / effective) * 100)
    h = history or {}
    return BudgetLineOut(
        id=str(line.id),
        year=line.year,
        month=line.month,
        qb_account_id=line.qb_account_id,
        account_name=line.account_name or (account_meta or {}).get("account_name"),
        account_type=(account_meta or {}).get("account_type"),
        amount=effective,
        line_type=line.line_type,
        pct_of_revenue=line.pct_of_revenue,
        source=line.source,
        is_locked=bool(line.is_locked),
        notes=line.notes,
        actual=actual,
        variance=variance,
        variance_pct=variance_pct,
        trailing_3mo_avg=Decimal(h.get("trailing_3mo_avg") or 0),
        trailing_6mo_avg=Decimal(h.get("trailing_6mo_avg") or 0),
        same_month_last_year=h.get("same_month_last_year"),
    )


# ---------- endpoints ----------


@router.get("", dependencies=[Depends(require_permission("accounting.read"))])
def list_budget_for_month(
    request: Request,
    year: int = Query(..., ge=2000, le=2999),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return all budget lines for one month, with actuals + variance.

    Also returns any expense accounts present in cached P&L that don't yet
    have a budget line — so the UI can render "add line" affordances
    without a second round trip.
    """
    tenant_id = _tenant_id(request)
    actuals = _actuals_for_month(db, year=year, month=month)
    monthly_revenue = _revenue_basis_for_month(db, year=year, month=month)

    lines = (
        db.query(MonthlyBudget)
        .filter(MonthlyBudget.year == year, MonthlyBudget.month == month)
        .order_by(MonthlyBudget.account_name)
        .all()
    )
    # Pull history for every account that appears on the page (budgeted or
    # not yet) in one batched query — single round trip, then bucketed in
    # Python. Lets the UI render trailing avgs + same-month-last-year
    # alongside the budget without an N+1.
    all_pnl_accounts = _expense_accounts_from_pnl(db)
    all_account_ids = sorted({a["qb_account_id"] for a in all_pnl_accounts}
                             | {ln.qb_account_id for ln in lines})
    history = _history_for_accounts(db, year=year, month=month, account_ids=all_account_ids)

    serialized: list[BudgetLineOut] = []
    budgeted_accounts: set[str] = set()
    for line in lines:
        meta = actuals.get(line.qb_account_id) or {}
        actual = Decimal(meta.get("amount") or 0)
        serialized.append(
            _serialize(
                line, actual,
                monthly_revenue=monthly_revenue,
                account_meta=meta,
                history=history.get(line.qb_account_id),
            )
        )
        budgeted_accounts.add(line.qb_account_id)

    # Available-to-budget accounts (in P&L but no line yet). Include their
    # history so the Add-line flow can show "3-mo avg: $X" hints.
    available: list[dict[str, Any]] = []
    for a in all_pnl_accounts:
        aid = a["qb_account_id"]
        if aid in budgeted_accounts:
            continue
        h = history.get(aid) or {}
        available.append({
            **a,
            "trailing_3mo_avg": str(h.get("trailing_3mo_avg") or 0),
            "trailing_6mo_avg": str(h.get("trailing_6mo_avg") or 0),
            "same_month_last_year": (str(h["same_month_last_year"])
                                     if h.get("same_month_last_year") is not None else None),
        })

    totals_budget = sum((bl.amount for bl in serialized), Decimal("0"))
    totals_actual = sum((bl.actual for bl in serialized), Decimal("0"))
    last_synced = _pnl_last_synced_at(db)
    return {
        "year": year,
        "month": month,
        "tenant_id": tenant_id,
        "lines": [bl.model_dump(mode="json") for bl in serialized],
        "available_accounts": available,
        "totals": {
            "budget": str(totals_budget),
            "actual": str(totals_actual),
            "variance": str(totals_actual - totals_budget),
            "monthly_revenue_forecast": str(monthly_revenue),
        },
        "pnl_last_synced_at": last_synced.isoformat() if last_synced else None,
    }


@router.get("/grid", dependencies=[Depends(require_permission("accounting.read"))])
def budget_grid(
    year: int = Query(..., ge=2000, le=2999),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Year-at-a-glance: 12 monthly columns × every budgeted/actual account."""
    actuals_by_month = {m: _actuals_for_month(db, year=year, month=m) for m in range(1, 13)}
    # Per-month revenue basis: actuals for past months, forecast for current+future.
    rev_by_month: dict[int, Decimal] = {
        m: _revenue_basis_for_month(db, year=year, month=m) for m in range(1, 13)
    }

    lines = (
        db.query(MonthlyBudget)
        .filter(MonthlyBudget.year == year)
        .order_by(MonthlyBudget.qb_account_id, MonthlyBudget.month)
        .all()
    )
    # Build {account_id: {month: {budget, actual}}}
    grid: dict[str, dict[int, dict[str, Any]]] = {}
    names: dict[str, str | None] = {}
    types: dict[str, str | None] = {}
    for line in lines:
        per_acct = grid.setdefault(line.qb_account_id, {})
        meta = actuals_by_month.get(line.month, {}).get(line.qb_account_id) or {}
        effective = _resolve_budget_amount(line, rev_by_month.get(line.month, Decimal("0")))
        actual = Decimal(meta.get("amount") or 0)
        per_acct[line.month] = {
            "budget": str(effective),
            "actual": str(actual),
            "line_id": str(line.id),
            "line_type": line.line_type,
            "is_locked": bool(line.is_locked),
        }
        names[line.qb_account_id] = line.account_name or meta.get("account_name")
        types[line.qb_account_id] = meta.get("account_type") or types.get(line.qb_account_id)

    # Also surface accounts that have actuals but no budget anywhere this year.
    for month, m_actuals in actuals_by_month.items():
        for aid, meta in m_actuals.items():
            if meta.get("account_type") not in EXPENSE_TYPES:
                continue
            per_acct = grid.setdefault(aid, {})
            if month not in per_acct:
                per_acct[month] = {
                    "budget": "0",
                    "actual": str(meta.get("amount") or 0),
                    "line_id": None,
                    "line_type": None,
                    "is_locked": False,
                }
            names.setdefault(aid, meta.get("account_name"))
            types.setdefault(aid, meta.get("account_type"))

    rows = []
    for aid, per_month in sorted(grid.items(), key=lambda kv: (types.get(kv[0]) or "", names.get(kv[0]) or "")):
        rows.append({
            "qb_account_id": aid,
            "account_name": names.get(aid),
            "account_type": types.get(aid),
            "months": per_month,
        })

    return {
        "year": year,
        "rows": rows,
        "revenue_by_month": {str(m): str(v) for m, v in rev_by_month.items()},
    }


@router.post("", dependencies=[Depends(require_permission("accounting.write"))])
def create_budget_line(
    payload: BudgetLineIn,
    request: Request,
    db: Session = Depends(get_db),
) -> BudgetLineOut:
    """Manually add a budget line. UI typically uses this when the account
    isn't in P&L yet (new account, future plan)."""
    _validate_line_type(payload.line_type)
    existing = (
        db.query(MonthlyBudget)
        .filter(
            MonthlyBudget.year == payload.year,
            MonthlyBudget.month == payload.month,
            MonthlyBudget.qb_account_id == payload.qb_account_id,
        )
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(409, "budget line already exists for that account/month")

    line = MonthlyBudget(
        id=str(uuid4()),
        year=payload.year,
        month=payload.month,
        qb_account_id=payload.qb_account_id,
        account_name=payload.account_name,
        amount=payload.amount,
        line_type=payload.line_type,
        pct_of_revenue=payload.pct_of_revenue,
        source="user",
        is_locked=False,
        notes=payload.notes,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    log_audit_event_sync(
        db, tenant_id=_tenant_id(request), user_id="api",
        action="monthly_budget.create", entity_type="monthly_budget",
        entity_id=str(line.id),
        details={"year": line.year, "month": line.month, "qb_account_id": line.qb_account_id},
    )
    monthly_revenue = _revenue_basis_for_month(db, year=line.year, month=line.month)
    actuals = _actuals_for_month(db, year=line.year, month=line.month)
    meta = actuals.get(line.qb_account_id) or {}
    return _serialize(line, Decimal(meta.get("amount") or 0),
                      monthly_revenue=monthly_revenue, account_meta=meta)


@router.patch("/{line_id}", dependencies=[Depends(require_permission("accounting.write"))])
def update_budget_line(
    line_id: str,
    payload: BudgetLineUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> BudgetLineOut:
    line = db.get(MonthlyBudget, line_id)
    if line is None:
        raise HTTPException(404, "budget line not found")
    changes: dict[str, Any] = {}
    if payload.amount is not None:
        line.amount = payload.amount
        changes["amount"] = str(payload.amount)
    if payload.line_type is not None:
        _validate_line_type(payload.line_type)
        line.line_type = payload.line_type
        changes["line_type"] = payload.line_type
    if payload.pct_of_revenue is not None:
        line.pct_of_revenue = payload.pct_of_revenue
        changes["pct_of_revenue"] = str(payload.pct_of_revenue)
    if payload.notes is not None:
        line.notes = payload.notes
    if payload.is_locked is not None:
        line.is_locked = payload.is_locked
        changes["is_locked"] = payload.is_locked
    # Any user edit promotes the line to source='user' so the next auto-seed
    # won't overwrite it.
    if changes:
        line.source = "user"
    db.commit()
    db.refresh(line)
    log_audit_event_sync(
        db, tenant_id=_tenant_id(request), user_id="api",
        action="monthly_budget.update", entity_type="monthly_budget",
        entity_id=str(line.id), details=changes,
    )
    monthly_revenue = _revenue_basis_for_month(db, year=line.year, month=line.month)
    actuals = _actuals_for_month(db, year=line.year, month=line.month)
    meta = actuals.get(line.qb_account_id) or {}
    return _serialize(line, Decimal(meta.get("amount") or 0),
                      monthly_revenue=monthly_revenue, account_meta=meta)


@router.delete("/{line_id}", dependencies=[Depends(require_permission("accounting.write"))])
def delete_budget_line(
    line_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, str]:
    line = db.get(MonthlyBudget, line_id)
    if line is None:
        raise HTTPException(404, "budget line not found")
    if line.is_locked:
        raise HTTPException(409, "line is locked — unlock before deleting")
    snap = {"year": line.year, "month": line.month, "qb_account_id": line.qb_account_id}
    db.delete(line)
    db.commit()
    log_audit_event_sync(
        db, tenant_id=_tenant_id(request), user_id="api",
        action="monthly_budget.delete", entity_type="monthly_budget",
        entity_id=line_id, details=snap,
    )
    return {"ok": "true"}


@router.post("/seed", dependencies=[Depends(require_permission("accounting.write"))])
def seed_budget(
    request: Request,
    year: int = Query(..., ge=2000, le=2999),
    month: int = Query(..., ge=1, le=12),
    lookback_months: int = Query(3, ge=1, le=12),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Starting-template fill: create budget lines for expense accounts that
    DON'T have a line yet, using the trailing average of P&L actuals.

    Hard rule: **never overwrites an existing line.** Any account with a
    budget line for (year, month) — whether locked or not, whether
    source=auto_seed or source=user — is left alone. This is a
    starting-template tool, not a re-seed.

    The 2026-05-24 audit + Doug feedback both flagged the prior
    overwrite_user_edits footgun. Removed entirely so there's no
    destructive path. Users who want to reset a line can delete it
    explicitly; the next seed will refill that empty slot.

    - Snap each seeded amount to nearest $10.
    - Always writes source='auto_seed', line_type='fixed'.
    - Per-line classification happens via the separate /classify endpoint.
    """
    months = []
    y, m = year, month
    for _ in range(lookback_months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        months.append((y, m))
    if not months:
        raise HTTPException(400, "lookback_months must be >= 1")

    # SUM by qb_account_id across the lookback window. Composite-key IN
    # is encoded as (year * 100 + month) so the query works on both
    # Postgres and SQLite (tests). Expanding bindparam keeps it parameterized.
    keys = [y * 100 + m for (y, m) in months]
    stmt = text(
        "SELECT qb_account_id, "
        "MAX(account_name) AS account_name, "
        "MAX(account_type) AS account_type, "
        "SUM(amount) AS total "
        "FROM qb_pnl_monthly "
        "WHERE (year * 100 + month) IN :keys "
        "AND account_type IN ('Expense','Cost of Goods Sold','Other Expense') "
        "GROUP BY qb_account_id"
    ).bindparams(bindparam("keys", expanding=True))
    rows = db.execute(stmt, {"keys": keys}).all()

    created = 0
    skipped_existing = 0
    # Batch-lookup which accounts already have a line — avoids N round-trips
    # to the DB inside the loop.
    existing_ids = {
        r[0] for r in db.execute(
            text(
                "SELECT qb_account_id FROM monthly_budgets "
                "WHERE year = :y AND month = :m"
            ),
            {"y": year, "m": month},
        ).all()
    }
    for r in rows:
        if r.qb_account_id in existing_ids:
            skipped_existing += 1
            continue
        avg = Decimal(r.total or 0) / Decimal(lookback_months)
        snapped = _snap_to_ten(avg)
        if snapped <= 0:
            continue
        db.add(MonthlyBudget(
            id=str(uuid4()),
            year=year, month=month,
            qb_account_id=r.qb_account_id,
            account_name=r.account_name,
            amount=snapped,
            line_type="fixed",
            source="auto_seed",
            is_locked=False,
        ))
        created += 1
    db.commit()

    log_audit_event_sync(
        db, tenant_id=_tenant_id(request), user_id="api",
        action="monthly_budget.seed", entity_type="monthly_budget", entity_id=f"{year}-{month:02d}",
        details={"created": created, "skipped_existing": skipped_existing,
                 "lookback_months": lookback_months},
    )
    return {
        "year": year, "month": month,
        "lookback_months": lookback_months,
        "created": created,
        "skipped_existing": skipped_existing,
    }


_KIND_TO_TYPES: dict[str, tuple[str, ...]] = {
    # Default for the page: everything that represents money going OUT.
    # For a service business like GDX this is critical — COGS (Contract
    # labor, Supplies & Materials, Subcontractors) is often the largest
    # spend bucket. The prior single-type filter hid COGS by default.
    "spending": ("Expense", "Cost of Goods Sold", "Other Expense"),
    "income": ("Income", "Other Income"),
    "all": ("Expense", "Cost of Goods Sold", "Other Expense", "Income", "Other Income"),
}


@router.get("/trends", dependencies=[Depends(require_permission("accounting.read"))])
def spending_trends(
    months: int = Query(24, ge=3, le=60),
    account_kind: str = Query("spending", pattern="^(spending|income|all)$"),
    account_type: str | None = Query(None, pattern="^(Expense|Cost of Goods Sold|Other Expense|Income|Other Income)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Spending history per account, last N months, for the trends view.

    Returns rows keyed by account_id with a sparse monthly series. Frontend
    plots a line per account.

    Two filter modes:
    - ``account_kind`` (preferred) — "spending" | "income" | "all". Default
      "spending" covers Expense + Cost of Goods Sold + Other Expense so the
      page shows the full picture of money going out (the prior single-type
      filter hid COGS, which is often the largest spend bucket for service
      businesses).
    - ``account_type`` (back-compat) — exact single-type match, overrides
      account_kind when present.
    """
    today = date.today()
    # Inclusive window — current month back N. Inline because the trailing
    # helper used elsewhere is exclusive of the anchor month.
    window: list[tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(months):
        window.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys = [y * 100 + m for (y, m) in window]
    types_filter = (account_type,) if account_type else _KIND_TO_TYPES[account_kind]
    stmt = text(
        "SELECT qb_account_id, account_name, account_type, year, month, amount "
        "FROM qb_pnl_monthly "
        "WHERE (year * 100 + month) IN :keys AND account_type IN :types "
        "ORDER BY account_type, account_name, year, month"
    ).bindparams(
        bindparam("keys", expanding=True),
        bindparam("types", expanding=True),
    )
    rows = db.execute(stmt, {"keys": keys, "types": list(types_filter)}).all()

    # Bucket → {account_id: {meta + series: [{year, month, amount}, ...]}}
    series: dict[str, dict[str, Any]] = {}
    for r in rows:
        bucket = series.setdefault(r.qb_account_id, {
            "qb_account_id": r.qb_account_id,
            "account_name": r.account_name,
            "account_type": r.account_type,
            "series": [],
        })
        bucket["series"].append({
            "year": int(r.year),
            "month": int(r.month),
            "amount": str(Decimal(r.amount or 0)),
        })

    # Stable ordering: account_type then account_name
    ordered = sorted(series.values(), key=lambda a: (a.get("account_type") or "", a.get("account_name") or ""))
    last_synced = _pnl_last_synced_at(db)
    return {
        "months": months,
        "as_of": today.isoformat(),
        "account_kind": account_kind,
        "account_type_filter": account_type,
        "account_types_included": list(types_filter),
        "accounts": ordered,
        "pnl_last_synced_at": last_synced.isoformat() if last_synced else None,
    }


@router.post("/{line_id}/lock", dependencies=[Depends(require_permission("accounting.write"))])
def lock_line(
    line_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    line = db.get(MonthlyBudget, line_id)
    if line is None:
        raise HTTPException(404, "budget line not found")
    line.is_locked = True
    db.commit()
    log_audit_event_sync(
        db, tenant_id=_tenant_id(request), user_id="api",
        action="monthly_budget.lock", entity_type="monthly_budget", entity_id=line_id, details={},
    )
    return {"id": line_id, "is_locked": True}


@router.post("/{line_id}/unlock", dependencies=[Depends(require_permission("accounting.write"))])
def unlock_line(
    line_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    line = db.get(MonthlyBudget, line_id)
    if line is None:
        raise HTTPException(404, "budget line not found")
    line.is_locked = False
    db.commit()
    log_audit_event_sync(
        db, tenant_id=_tenant_id(request), user_id="api",
        action="monthly_budget.unlock", entity_type="monthly_budget", entity_id=line_id, details={},
    )
    return {"id": line_id, "is_locked": False}


# ---------- classifier (Slice 3) ----------

# Keyword → line_type. Lowercased; matched via substring on account_name.
_KEYWORDS_FIXED: tuple[str, ...] = (
    "rent", "insurance", "subscription", "software", "telephone", "telecom",
    "internet", "lease", "mortgage", "license", "dues", "membership",
    "depreciation", "amortization", "payroll service",
)
_KEYWORDS_VARIABLE: tuple[str, ...] = (
    "material", "fuel", "gasoline", "diesel", "parts", "supplies", "freight",
    "shipping", "subcontract", "merchant", "credit card fee", "commission",
    "advertising", "marketing",
)


def _kw_match(name: str, kws: tuple[str, ...]) -> str | None:
    low = name.lower()
    for kw in kws:
        if kw in low:
            return kw
    return None


def _classify_one(name: str | None, monthly_amounts: list[Decimal]) -> dict[str, Any]:
    """Decide fixed vs variable for one account.

    Returns {proposed, reason}. Falls back to 'fixed' if signal is weak.
    """
    name = name or ""
    hit = _kw_match(name, _KEYWORDS_FIXED)
    if hit:
        return {"proposed": "fixed", "reason": f"name match '{hit}'"}
    hit = _kw_match(name, _KEYWORDS_VARIABLE)
    if hit:
        return {"proposed": "variable", "reason": f"name match '{hit}'"}
    nonzero = [float(a) for a in monthly_amounts if a > 0]
    if len(nonzero) < 3:
        return {"proposed": "fixed", "reason": "insufficient history (<3 months)"}
    mean = sum(nonzero) / len(nonzero)
    if mean == 0:
        return {"proposed": "fixed", "reason": "zero mean"}
    var = sum((x - mean) ** 2 for x in nonzero) / len(nonzero)
    cv = (var ** 0.5) / mean
    if cv > 0.30:
        return {"proposed": "variable", "reason": f"CV={cv:.2f} > 0.30"}
    return {"proposed": "fixed", "reason": f"CV={cv:.2f} <= 0.30"}


@router.post("/classify", dependencies=[Depends(require_permission("accounting.read"))])
def classify_accounts(
    lookback_months: int = Query(6, ge=3, le=24),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Propose line_type per expense account. Does NOT write — UI confirms."""
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(lookback_months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        months.append((y, m))

    keys = [y * 100 + m for (y, m) in months]
    stmt = text(
        "SELECT qb_account_id, year, month, account_name, account_type, amount "
        "FROM qb_pnl_monthly "
        "WHERE (year * 100 + month) IN :keys "
        "AND account_type IN ('Expense','Cost of Goods Sold','Other Expense') "
        "ORDER BY qb_account_id, year, month"
    ).bindparams(bindparam("keys", expanding=True))
    rows = db.execute(stmt, {"keys": keys}).all()

    # Bucket monthly amounts per account.
    per_acct: dict[str, dict[str, Any]] = {}
    for r in rows:
        bucket = per_acct.setdefault(r.qb_account_id, {
            "account_name": r.account_name,
            "account_type": r.account_type,
            "amounts": [],
        })
        bucket["amounts"].append(Decimal(r.amount or 0))

    # Existing budget lines tell us current_line_type for any month.
    current_types: dict[str, str] = {}
    cur_rows = db.execute(
        text("SELECT qb_account_id, MAX(line_type) AS lt FROM monthly_budgets GROUP BY qb_account_id")
    ).all()
    for r in cur_rows:
        current_types[r.qb_account_id] = r.lt

    proposals: list[dict[str, Any]] = []
    for aid, b in per_acct.items():
        decision = _classify_one(b["account_name"], b["amounts"])
        proposals.append({
            "qb_account_id": aid,
            "account_name": b["account_name"],
            "account_type": b["account_type"],
            "current_line_type": current_types.get(aid),
            "proposed_line_type": decision["proposed"],
            "reason": decision["reason"],
            "months_of_history": len(b["amounts"]),
        })
    proposals.sort(key=lambda p: (p["account_type"] or "", p["account_name"] or ""))
    return {"lookback_months": lookback_months, "proposals": proposals}


# ---------- refresh actuals from QBO ----------


@router.post("/refresh-actuals", dependencies=[Depends(require_permission("accounting.write"))])
async def refresh_actuals(
    request: Request,
    year: int = Query(..., ge=2000, le=2999),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Pull a fresh QBO ProfitAndLoss for `year` and refresh the cache.

    One QBO call returns all 12 months × every P&L account.
    """
    tenant_id = _tenant_id(request)
    # Tenant's chosen accounting basis. Default Accrual matches QBO's
    # default report basis. Cash-basis only counts paid items.
    row = db.query(AppSettings).first()
    accounting_method = (getattr(row, "qb_accounting_method", None) or "Accrual") if row else "Accrual"
    if accounting_method not in ("Cash", "Accrual"):
        accounting_method = "Accrual"
    try:
        async with await get_qb_client(tenant_id, db) as qb:
            result = await pull_profit_and_loss(
                tenant_id, db, qb, year=year, accounting_method=accounting_method,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # Auditor 2026-05-24: never pipe str(exc) into the response — token
        # decryption errors and internal URLs can leak. Log full exception
        # server-side; return a generic message to the caller.
        log.exception("budget_refresh_actuals_failed tenant=%s year=%d", tenant_id, year)
        raise HTTPException(
            502,
            "QuickBooks ProfitAndLoss fetch failed. See server logs for details.",
        ) from exc
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id="api",
        action="monthly_budget.refresh_actuals", entity_type="qb_pnl_monthly",
        entity_id=str(year), details=result,
    )
    return result


# ─── Fix-in-QuickBooks panel (Sprint 2026-05-25) ─────────────────────


def _load_qb_accounts(db: Session) -> list[dict[str, Any]]:
    """Read the tenant's QB chart of accounts. Returns [] if not synced yet."""
    try:
        rows = db.execute(
            text(
                "SELECT qb_account_id, name, account_type, account_sub_type, "
                "current_balance, active "
                "FROM qb_accounts ORDER BY account_type, name"
            )
        ).all()
    except Exception:  # noqa: BLE001
        log.exception("qb_accounts_load_failed")
        return []
    return [
        {
            "qb_account_id": r.qb_account_id,
            "name": r.name,
            "account_type": r.account_type,
            "account_sub_type": r.account_sub_type,
            "current_balance": str(r.current_balance) if r.current_balance is not None else None,
            "active": bool(r.active) if r.active is not None else True,
        }
        for r in rows
    ]


@router.get("/anomalies", dependencies=[Depends(require_permission("accounting.read"))])
async def list_anomalies(
    request: Request,
    year: int = Query(..., ge=2000, le=2999),
    account_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """For each expense account that totals net-negative for the year, pull
    its QBO ProfitAndLossDetail and surface the individual transactions
    that look miscategorized. Each transaction gets a suggested target
    account (or "open_in_qb" for transfers).

    If account_id is supplied, only inspects that one account (faster +
    matches the per-row "Review anomalies" UX).
    """
    tenant_id = _tenant_id(request)
    accounting_method = "Accrual"
    settings_row = db.query(AppSettings).first()
    if settings_row and getattr(settings_row, "qb_accounting_method", None) in ("Cash", "Accrual"):
        accounting_method = settings_row.qb_accounting_method

    # Find anomalous accounts (net-negative across the year) from cache.
    where_acct = ""
    params: dict[str, Any] = {"y": year}
    if account_id:
        where_acct = " AND qb_account_id = :aid "
        params["aid"] = account_id
    anomaly_rows = db.execute(
        text(
            "SELECT qb_account_id, MAX(account_name) AS account_name, "
            "MAX(account_type) AS account_type, SUM(amount) AS total "
            "FROM qb_pnl_monthly "
            f"WHERE year = :y {where_acct} "
            "AND account_type IN ('Expense','Cost of Goods Sold','Other Expense') "
            "GROUP BY qb_account_id "
            "HAVING SUM(amount) < 0"
        ),
        params,
    ).all()

    if not anomaly_rows:
        return {
            "year": year, "tenant_id": tenant_id,
            "accounts": [], "qb_accounts": _load_qb_accounts(db),
            "accounting_method": accounting_method,
        }

    # Hard cap on per-request QBO ProfitAndLossDetail fetches. Each call is
    # ~3-8s on a busy account; a tenant with 10+ anomalies would otherwise
    # blow past the frontend's 30s default timeout AND chew through QBO
    # rate limits. The UI only ever asks for ONE account_id at a time, so
    # the cap really only matters when a script hits the multi-account
    # path. Auditor 2026-05-25.
    MAX_ANOMALIES_PER_REQUEST = 8
    if len(anomaly_rows) > MAX_ANOMALIES_PER_REQUEST and not account_id:
        anomaly_rows = list(anomaly_rows)[:MAX_ANOMALIES_PER_REQUEST]

    qb_accounts = _load_qb_accounts(db)
    out_accounts: list[dict[str, Any]] = []

    try:
        async with await get_qb_client(tenant_id, db) as qb:
            for anom in anomaly_rows:
                # Pull the YTD detail for this single account.
                detail = await fetch_profit_and_loss_detail(
                    qb,
                    start_date=f"{year:04d}-01-01",
                    end_date=f"{year:04d}-12-31",
                    account_id=anom.qb_account_id,
                    accounting_method=accounting_method,
                )
                txns = parse_profit_and_loss_detail(detail)
                # Per-transaction suggestion.
                suggested = []
                for t in txns:
                    s = suggest_target_account(
                        txn_type=t["txn_type"],
                        vendor_name=t["vendor_name"],
                        memo=t["memo"],
                        amount=t["amount"],
                        accounts=qb_accounts,
                    )
                    suggested.append({**t, "amount": str(t["amount"]), "suggestion": s})
                out_accounts.append({
                    "qb_account_id": anom.qb_account_id,
                    "account_name": anom.account_name,
                    "account_type": anom.account_type,
                    "total": str(Decimal(anom.total or 0)),
                    "transactions": suggested,
                })
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("anomalies_failed tenant=%s year=%d", tenant_id, year)
        raise HTTPException(
            502,
            "QuickBooks ProfitAndLossDetail fetch failed. See server logs for details.",
        ) from exc

    return {
        "year": year, "tenant_id": tenant_id,
        "accounts": out_accounts, "qb_accounts": qb_accounts,
        "accounting_method": accounting_method,
    }


class RecategorizeIn(BaseModel):
    txn_id: str = Field(..., min_length=1)
    txn_type: str = Field(..., min_length=1)
    new_account_id: str = Field(..., min_length=1)


@router.post("/recategorize", dependencies=[Depends(require_permission("accounting.write"))])
async def recategorize_one(
    payload: RecategorizeIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Apply one recategorization in QuickBooks. Yellow-tier: UI proposes,
    user confirms, this endpoint writes. Audit-logged with before/after.

    On success, the caller should immediately re-run `/api/budgets/refresh-actuals`
    to refresh the cached qb_pnl_monthly numbers.
    """
    tenant_id = _tenant_id(request)
    if payload.txn_type not in SUPPORTED_TYPES:
        raise HTTPException(
            400,
            f"recategorize: {payload.txn_type} not supported in v1 — fix in QB UI",
        )

    # Idempotency key derived from tenant + txn + target — prevents
    # double-application on a retry hitting Intuit's 24h request-id dedupe.
    idempotency_key = f"recat-{tenant_id}-{payload.txn_id}-{payload.new_account_id}"

    try:
        async with await get_qb_client(tenant_id, db) as qb:
            result = await recategorize_transaction(
                qb,
                txn_type=payload.txn_type,
                txn_id=payload.txn_id,
                new_account_id=payload.new_account_id,
                idempotency_key=idempotency_key,
            )
    except RecategorizeError as exc:
        # User-visible reason (no internal trace leakage).
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "recategorize_failed tenant=%s type=%s id=%s",
            tenant_id, payload.txn_type, payload.txn_id,
        )
        raise HTTPException(
            502,
            "QuickBooks recategorize failed. See server logs for details.",
        ) from exc

    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id="api",
        action="qb.recategorize", entity_type=payload.txn_type,
        entity_id=payload.txn_id, details=result,
    )
    return result
