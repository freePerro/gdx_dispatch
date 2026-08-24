from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_ as sa_and
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobCloseout,
    Payment,
    WarrantyClaim,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_module("reports_advanced"))])


def _parse_date(value: str | None, field_name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        log.exception("report_date_parse_failed")
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD") from exc


def _resolve_date_range(start_date: str | None, end_date: str | None) -> tuple[str, str, date]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    now = datetime.now(UTC)
    resolved_end = end or now.date()
    resolved_start = start or (resolved_end - timedelta(days=29))
    if resolved_start > resolved_end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")

    start_dt = datetime.combine(resolved_start, time.min, tzinfo=UTC)
    end_dt_exclusive = datetime.combine(resolved_end + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt.isoformat(), end_dt_exclusive.isoformat(), resolved_end


# Invoice statuses counted as "revenue billed" (excludes draft + void).
# Reconciles with the dashboard fallback path's intent — what the business
# can collect on, not the gross sum of every row in the invoices table.
_BILLED_STATUSES = ("sent", "paid", "overdue")

# M8 (money-audit-2026-08-04, HIGH/CONFIRMED) — one definition of "revenue".
#
# `Invoice.total_amount` was nullable and NO insert path ever wrote it: NULL on
# all 349 prod rows on 2026-08-22, so every surface that summed the bare column
# reported $0 against $829,164.66 of real billed work. `_summary_window` was
# fixed alone on 2026-04-27 and its four siblings were missed — which is exactly
# how the bug survived. The column is now DROPPED (migration 073) so it cannot
# be read again; these helpers exist so the NEXT revenue surface inherits the
# rule instead of re-deriving half of it.
#
# DEPOSITS ARE COUNTED. An earlier draft of this fix excluded
# billing_type='deposit' on the theory that a deposit and its final invoice
# bill the same work twice. That is false in this codebase and the adversarial
# audit caught it: `modules/deposits/service.py` rule 2 — "the final invoice
# nets the deposit with a negative line ... so deposit revenue + final revenue
# net to exactly the job's true total — no 150% double-count"
# (`service.py:503` writes "Less deposit paid — <n>"; prod INV-000354 carries
# -2936.49 against INV-000353). De-duplication already happens at invoice
# creation, so filtering deposits out here would subtract them a SECOND time —
# and of the five non-void deposits on prod only ONE has a netting sibling, so
# the other four (including a PAID $6,793.04) would have been deleted from
# revenue outright. Do not re-add the filter.
_BILLED_STATUS_SQL = ", ".join(f"'{s}'" for s in _BILLED_STATUSES)


def _alias_prefix(alias: str) -> str:
    """`"i"` -> `"i."`, `""` -> `""`, anything else -> ValueError.

    These fragments are interpolated into SQL, so the alias is the one input
    that could carry injection if a future caller ever sourced it from a
    request. Whitelisting the shape here is what makes the `# noqa: S608` on
    the call sites an actual guarantee rather than a promise.
    """
    if not alias:
        return ""
    if not alias.isidentifier():
        raise ValueError(f"unsafe SQL alias: {alias!r}")
    return f"{alias}."


def _revenue_amount_sql(alias: str = "") -> str:
    """The invoice amount for raw-SQL revenue aggregations.

    Was `COALESCE(total_amount, total)` while `total_amount` still existed —
    NULL on every row, which is what M8 was. The column is dropped (migration
    073); `total` is NOT NULL and is the amount.
    """
    p = _alias_prefix(alias)
    return f"{p}total"


def _revenue_where_sql(alias: str = "") -> str:
    """Shared WHERE fragment: not deleted, and billed (excludes draft + void)."""
    p = _alias_prefix(alias)
    return f"{p}deleted_at IS NULL AND {p}status IN ({_BILLED_STATUS_SQL})"


def _revenue_date_sql(alias: str = "") -> str:
    """Raw-SQL twin of `_revenue_date_expr()`: when the work was BILLED.

    `created_at` is when the row was inserted, which for imported invoices is
    the import run — 278 QB invoices spanning 2024-2026 all carry
    created_at = 2026-03-29. Grouping revenue on it draws a $607,419.52 spike
    on the import date and empties the months the work was actually billed in.
    `_summary_window` moved to invoice_date on 2026-04-27 (Phase D) and the
    raw-SQL surfaces were missed — the same sibling gap as `total_amount`.

    invoice_date is nullable on legacy rows, so fall back to created_at.
    """
    p = _alias_prefix(alias)
    return f"COALESCE({p}invoice_date, CAST({p}created_at AS DATE))"


def _credits_by_period(db: Session, period: str, start_ts, end_ts) -> dict:
    """Σ credit MEMOS per period, keyed by truncated date.

    `credit_applied` is deliberately excluded. It is a settlement event, not a
    revenue reduction: the credit memo that minted the customer credit already
    reduced revenue, and applying that credit to another invoice would subtract
    it a second time. The ledger agrees — `modules/ledger/reports.py::_cash_events`
    signs `credit_applied` **+1**, grouping it with payments and refunds as cash,
    while plain credit memos are not cash events at all. (`_recalculate_invoice`
    does subtract both, correctly, because it is computing BALANCE, not revenue.)

    M19 (money-audit-2026-08-04): `Invoice.total` is never reduced by a credit
    memo — only `balance_due` is — and no revenue aggregate joined
    `invoice_adjustments`, so a credited invoice kept counting at full value.

    Attributed to the date the credit was ISSUED, which is what the GL does:
    `post_credit_memo` posts at `effective_at = adjustment.created_at.date()`
    (modules/ledger/rules.py). The ledger is the book of record, so the reports
    match it rather than inventing a second convention.

    NOTE M19's other prescription — "exclude billing_type='deposit' from billed
    revenue" — is WRONG here and deliberately not implemented. The final invoice
    already nets the deposit with a negative line, so excluding it subtracts the
    deposit twice. Verified on the one real pair on prod: deposit $3,112.61 +
    final $3,288.74 − credits $176.12 = $6,225.23, exactly the job's true value;
    excluding the deposit gives $3,288.74. Netting credits is the whole fix.
    """
    rows = db.execute(
        text(
            """
            SELECT date_trunc(:period, a.created_at) AS period_start,
                   COALESCE(SUM(a.amount), 0) AS credited
            FROM invoice_adjustments a
            JOIN invoices i ON i.id = a.invoice_id
            WHERE a.kind = 'credit_memo'
              -- M18 audit round 2, same shape swept here (the only two
              -- queries netting invoice_adjustments against a status-
              -- filtered invoice universe live in this file): a voided
              -- invoice's revenue leaves the universe — its credit memo
              -- must stop netting too. Deposits deliberately stay (the
              -- docstring proves excluding them double-subtracts here).
              AND i.status != 'void'
              AND i.deleted_at IS NULL
              AND a.created_at >= :start_ts
              AND a.created_at < :end_ts
            GROUP BY date_trunc(:period, a.created_at)
            """
        ),
        {"period": period, "start_ts": start_ts, "end_ts": end_ts},
    ).mappings().all()
    return {_iso_or_none(r["period_start"]): float(r["credited"] or 0) for r in rows}


def _credits_total(db: Session, start_ts, end_ts) -> float:
    """Σ credits issued in the window — the scalar form of `_credits_by_period`."""
    got = db.execute(
        text(
            """
            SELECT COALESCE(SUM(a.amount), 0)
            FROM invoice_adjustments a
            JOIN invoices i ON i.id = a.invoice_id
            WHERE a.kind = 'credit_memo'
              AND i.deleted_at IS NULL
              AND a.created_at >= :start_ts
              AND a.created_at < :end_ts
            """
        ),
        {"start_ts": start_ts, "end_ts": end_ts},
    ).scalar_one_or_none()
    return float(got or 0)


def _iso_or_none(value):
    """Raw-SQL date columns come back as datetime on Postgres and as `str` on
    SQLite. Both reach these serializers, so coerce instead of assuming — a
    bare `.isoformat()` made `/top-customers` and `/revenue-by-period`
    untestable on the SQLite harness (and would 500 on any SQLite-backed run).
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _revenue_orm_filters() -> tuple:
    """ORM equivalent of `_revenue_where_sql` — same terms, same order."""
    return (
        Invoice.deleted_at.is_(None),
        Invoice.status.in_(_BILLED_STATUSES),
    )

# Lifecycle stages considered "open" — anything not terminal.
# Source of truth is jobs.lifecycle_stage (Enum), not jobs.status (legacy
# nullable varchar that imports leave NULL).
# Includes legacy "lead" for any tenant whose 2026-05-13 backfill hasn't run yet —
# pre-rename rows should still count as open work.
_OPEN_LIFECYCLE_STAGES = ("lead", "service_call", "estimate", "scheduled", "in_progress")


def _revenue_date_expr():
    # COALESCE(invoice_date, created_at::date) — invoice_date is the
    # business-facing "when this invoice happened" but is nullable on
    # legacy/imported rows. Fall back to created_at so older rows aren't
    # silently excluded from the window.
    return func.coalesce(Invoice.invoice_date, func.cast(Invoice.created_at, Invoice.invoice_date.type))


def _summary_window(db: Session, start_dt: str, end_dt: str, today: date | None = None) -> dict:
    # D99 follow-up (an earlier session): every other reports query coalesces
    # `total_amount` (nullable, almost never populated by any insert path)
    # back to `total` (NOT NULL, default 0). _summary_window was the lone
    # outlier — summed total_amount only, which is null on every prod row,
    # so Dashboard Revenue read $0 against $712k of real billed work.
    #
    # Phase D audit 2026-04-27: filter on invoice_date (business semantics)
    # not created_at (import-import semantics). Also: jobs.status is the
    # legacy varchar — null on QB-imported rows. Use lifecycle_stage.
    _amount = Invoice.total
    _start_d = date.fromisoformat(start_dt[:10])
    _end_d = date.fromisoformat(end_dt[:10])  # exclusive day
    _rev_date = _revenue_date_expr()
    revenue_row = db.execute(
        select(
            func.coalesce(func.sum(_amount), 0).label("revenue_total"),
            func.coalesce(func.avg(_amount), 0).label("avg_job_value"),
        ).where(
            # M8: routed through the shared definition so the KPI card and the
            # "Revenue by Period" chart can never drift apart. Behaviour here is
            # UNCHANGED — these are the same two terms this query already had.
            # (This also feeds DashboardView's "Revenue Billed" tile, so a real
            # rule change here would move a second screen, not just Reports.)
            *_revenue_orm_filters(),
            _rev_date >= _start_d,
            _rev_date < _end_d,
        )
    ).mappings().first()
    # Jobs completed in the window: completed_at preferred; fall back to
    # created_at when imports left it null (rare since import sets
    # lifecycle_stage='completed' but no completed_at).
    _completed_at = func.coalesce(Job.completed_at, Job.created_at)
    jobs_completed = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            _completed_at >= start_dt,
            _completed_at < end_dt,
        )
    ) or 0
    # Open jobs: a backlog count, not window-scoped. The dashboard label
    # "Open Jobs" means "right now, how many are open" — filtering by a
    # 30-day window made the count drop to 0 whenever older rows held the
    # actual backlog.
    open_jobs = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage.in_(_OPEN_LIFECYCLE_STAGES),
        )
    ) or 0
    # True overdue: balance_due > 0 AND due_date < today AND not paid/void.
    # The legacy `Invoice.status == "overdue"` filter returned 0 on prod
    # because the QB import never sets that status. Computing from the
    # underlying columns matches what /billing's "Overdue" tab shows.
    today = today or datetime.now(UTC).date()
    overdue_invoices = db.scalar(
        select(func.count()).where(
            Invoice.deleted_at.is_(None),
            Invoice.status.notin_(("paid", "void", "draft")),
            Invoice.balance_due > 0,
            Invoice.due_date.is_not(None),
            Invoice.due_date < today,
        )
    ) or 0
    return {
        # M19: the KPI tile nets credits for the same reason the chart beside
        # it does — a credit memo reduces balance_due and never touches
        # Invoice.total, so a credited invoice kept counting at full value.
        # This also feeds DashboardView's "Revenue Billed" tile.
        "revenue_total": float((revenue_row or {}).get("revenue_total", 0) or 0)
        - _credits_total(db, start_dt, end_dt),
        "avg_job_value": float((revenue_row or {}).get("avg_job_value", 0) or 0),
        "jobs_completed": int(jobs_completed),
        "open_jobs": int(open_jobs),
        "overdue_invoices": int(overdue_invoices),
    }


def _trend_pct(current: float, previous: float) -> float | None:
    """Period-over-period % change, with two guards.

    1. Near-zero prior: a $100 prior with $709k current produces a
       709,468% trend — mathematically valid, business-meaningless.
    2. Magnitude blowout: even when prior clears the floor, capping at
       a 1000% swing prevents misleading double-digit-thousand renders.

    Returns None when either guard fires; the frontend renders `—`.
    """
    if previous is None or current is None:
        return None
    # Floor: prior periods below this contribute too little signal.
    # 100 works for both currency ($100) and counts (100 jobs); a single
    # outlier invoice in the prior window won't drive a meaningful trend.
    if abs(previous) < 100:
        return None
    pct = ((current - previous) / previous) * 100
    # Cap the rendered magnitude — anything beyond 1000% communicates
    # nothing more useful than "huge swing", show `—` instead.
    if abs(pct) > 1000:
        return None
    return round(pct, 1)


@router.get("/summary", response_model=None)
def reports_summary(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    start_dt, end_dt, end_resolved = _resolve_date_range(start_date, end_date)
    # Previous-window of equal length for trend deltas.
    start_d = date.fromisoformat(start_dt[:10])
    end_d = date.fromisoformat(end_dt[:10])
    window_days = (end_d - start_d).days
    prev_start_dt = datetime.combine(start_d - timedelta(days=window_days), time.min, tzinfo=UTC).isoformat()
    prev_end_dt = start_dt

    cur = _summary_window(db, start_dt, end_dt)
    prev = _summary_window(db, prev_start_dt, prev_end_dt)
    return {
        **cur,
        "revenue_trend": _trend_pct(cur["revenue_total"], prev["revenue_total"]),
        "open_jobs_trend": _trend_pct(cur["open_jobs"], prev["open_jobs"]),
        "overdue_invoices_trend": _trend_pct(cur["overdue_invoices"], prev["overdue_invoices"]),
        "jobs_completed_trend": _trend_pct(cur["jobs_completed"], prev["jobs_completed"]),
        "range": {
            "start_date": start_d.isoformat(),
            "end_date": end_resolved.isoformat(),
        },
    }


@router.get("/top-customers", response_model=None)
def top_customers(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Top customers by total invoiced revenue in the date range.

    Closes ReportsView.vue gap. Returns {items: [...], total, range}.

    When neither date is supplied and the default 30-day window yields
    no rows, retry with a 90-day window so a tenant whose latest
    invoice is older than a month still sees their list. Explicit
    ranges are honored as-is — no auto-widen.
    """
    user_supplied_range = bool(start_date or end_date)
    start_dt, end_dt, end_date_resolved = _resolve_date_range(start_date, end_date)
    sql = text(
        f"""
        SELECT
            c.id AS customer_id,
            c.name AS customer_name,
            c.email AS customer_email,
            c.phone AS customer_phone,
            COUNT(DISTINCT i.id) AS invoice_count,
            COUNT(DISTINCT j.id) AS job_count,
            COALESCE(SUM({_revenue_amount_sql("i")}), 0) AS total_revenue,
            COALESCE(AVG({_revenue_amount_sql("i")}), 0) AS avg_invoice,
            MAX(i.created_at) AS last_invoice_at
        FROM invoices i
        LEFT JOIN jobs j ON j.id = i.job_id
        LEFT JOIN customers c ON c.id = COALESCE(i.customer_id, j.customer_id) AND c.deleted_at IS NULL
        WHERE {_revenue_where_sql("i")}
          AND {_revenue_date_sql("i")} >= :start_d
          AND {_revenue_date_sql("i")} < :end_d
          AND c.id IS NOT NULL
        GROUP BY c.id, c.name, c.email, c.phone
        ORDER BY total_revenue DESC NULLS LAST
        LIMIT :limit
        """  # noqa: S608 — interpolation is constants + whitelisted alias (_alias_prefix); user input is bound
    )
    try:
        rows = db.execute(sql, {"start_d": date.fromisoformat(start_dt[:10]), "end_d": date.fromisoformat(end_dt[:10]), "limit": limit}).mappings().all()
        if not rows and not user_supplied_range:
            # Widen to 90 days for the no-data default case.
            wider_start = datetime.combine(
                end_date_resolved - timedelta(days=89), time.min, tzinfo=UTC
            ).isoformat()
            rows = db.execute(
                sql, {"start_dt": wider_start, "end_dt": end_dt, "limit": limit}
            ).mappings().all()
            if rows:
                start_dt = wider_start
    except Exception:
        log.exception("top_customers_failed")
        raise HTTPException(status_code=500, detail="Unable to compute top customers") from None

    items = [
        {
            "customer_id": str(r["customer_id"]) if r["customer_id"] else None,
            "customer_name": r["customer_name"] or "(unassigned)",
            "customer_email": r["customer_email"],
            "customer_phone": r["customer_phone"],
            "invoice_count": int(r["invoice_count"] or 0),
            "job_count": int(r["job_count"] or 0),
            "total_revenue": float(r["total_revenue"] or 0),
            "lifetime_value": float(r["total_revenue"] or 0),
            "avg_invoice": float(r["avg_invoice"] or 0),
            "last_invoice_at": _iso_or_none(r["last_invoice_at"]),
        }
        for r in rows
    ]
    return {
        "items": items,
        "total": len(items),
        "range": {"start": start_dt, "end": end_dt, "end_date": end_date_resolved.isoformat()},
    }


@router.get("/revenue-by-period", response_model=None)
def revenue_by_period(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    period: str = Query(default="month", pattern="^(day|week|month)$"),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Revenue aggregated by day / week / month for charting.

    Closes ReportsView.vue gap. Returns {items: [{period_start, revenue,
    invoice_count, avg_invoice}], period, range}.
    """
    start_dt, end_dt, end_date_resolved = _resolve_date_range(start_date, end_date)
    # Postgres date_trunc supports day/week/month directly — pass the literal
    # through a whitelist check (done by FastAPI pattern validator on period).
    try:
        rows = db.execute(
            text(
                f"""
                SELECT
                    date_trunc(:period, {_revenue_date_sql()}) AS period_start,
                    COUNT(*) AS invoice_count,
                    COALESCE(SUM({_revenue_amount_sql()}), 0) AS revenue,
                    COALESCE(AVG({_revenue_amount_sql()}), 0) AS avg_invoice
                FROM invoices
                WHERE {_revenue_where_sql()}
                  AND {_revenue_date_sql()} >= :start_d
                  AND {_revenue_date_sql()} < :end_d
                GROUP BY date_trunc(:period, {_revenue_date_sql()})
                ORDER BY period_start ASC
                """  # noqa: S608 — interpolation is constants only; user input is bound
            ),
            {
                "period": period,
                "start_d": date.fromisoformat(start_dt[:10]),
                "end_d": date.fromisoformat(end_dt[:10]),
            },
        ).mappings().all()
    except Exception:
        log.exception("revenue_by_period_failed")
        raise HTTPException(status_code=500, detail="Unable to compute revenue by period") from None

    # M19: net balance-reducing adjustments out of each period. Without this a
    # credited invoice keeps counting at full value, because a credit memo
    # reduces balance_due and never touches Invoice.total.
    _credits = _credits_by_period(db, period, start_dt, end_dt)
    items = []
    for r in rows:
        key = _iso_or_none(r["period_start"])
        gross = float(r["revenue"] or 0)
        credited = _credits.pop(key, 0.0)
        count = int(r["invoice_count"] or 0)
        net = gross - credited
        items.append(
            {
                "period_start": key,
                "invoice_count": count,
                "revenue": net,
                "gross_revenue": gross,
                "credits": credited,
                # Average invoice BILLED — gross over count. Dividing net by
                # count would report a negative "average invoice" for any
                # period whose credits exceed its billings, which is not a
                # number that means anything.
                "avg_invoice": (gross / count) if count else 0.0,
            }
        )
    # A period with credits but no invoices billed still reduced revenue; it
    # must appear rather than silently vanishing.
    for key, credited in _credits.items():
        items.append(
            {
                "period_start": key,
                "invoice_count": 0,
                "revenue": -credited,
                "gross_revenue": 0.0,
                "credits": credited,
                "avg_invoice": 0.0,
            }
        )
    items.sort(key=lambda i: i["period_start"] or "")
    return {
        "items": items,
        "period": period,
        "total_revenue": sum(i["revenue"] for i in items),
        "range": {"start": start_dt, "end": end_dt, "end_date": end_date_resolved.isoformat()},
    }


def _rollup_sales_tax(rows) -> tuple[list[dict], dict]:
    """Fold per-(period, source) tax rows into per-period cards + grand totals.

    Pure over the SQL result so the money math is testable without Postgres
    (the query's date_trunc is PG-only). Each input row is a mapping with
    period_start (date|None), source ('gdx'|'quickbooks'), tax_total,
    tax_collected, invoice_count, taxable_base. Outstanding is derived, never
    trusted from input — total minus collected, so it can't disagree.
    """
    def _empty_source() -> dict:
        return {"tax_total": 0.0, "tax_collected": 0.0, "invoice_count": 0}

    periods: dict[str, dict] = {}
    for r in rows:
        ps = r["period_start"]
        key = ps.isoformat() if hasattr(ps, "isoformat") else (str(ps) if ps else "unknown")
        p = periods.setdefault(key, {
            "period_start": key,
            "gdx": _empty_source(),
            "quickbooks": _empty_source(),
        })
        if r.get("row_kind") == "adjustments":
            # M18 companion rows: per-period tax given back, split by kind.
            p["tax_credited"] = float(r.get("tax_credited") or 0) + float(p.get("tax_credited") or 0)
            p["tax_refunded"] = float(r.get("tax_refunded") or 0) + float(p.get("tax_refunded") or 0)
            continue
        src = r["source"] if r["source"] in ("gdx", "quickbooks") else "quickbooks"
        p[src] = {
            "tax_total": float(r["tax_total"] or 0),
            "tax_collected": float(r["tax_collected"] or 0),
            "invoice_count": int(r["invoice_count"] or 0),
        }
    items = sorted(periods.values(), key=lambda p: p["period_start"])
    for p in items:
        # M18 (Doug 2026-08-24: pro-rata at the invoice's rate): net the tax
        # given back. Credits reduce the LIABILITY (tax_total) in the period
        # the credit was issued — mirroring _credits_by_period's revenue
        # treatment; a remitted month is never restated. Refunds return cash,
        # so they reduce liability AND collected, leaving outstanding alone.
        credited = round(float(p.get("tax_credited") or 0), 2)
        refunded = round(float(p.get("tax_refunded") or 0), 2)
        p["tax_credited"] = credited
        p["tax_refunded"] = refunded
        p["tax_total"] = round(
            p["gdx"]["tax_total"] + p["quickbooks"]["tax_total"] - credited - refunded, 2
        )
        p["tax_collected"] = round(
            p["gdx"]["tax_collected"] + p["quickbooks"]["tax_collected"] - refunded, 2
        )
        p["tax_outstanding"] = round(p["tax_total"] - p["tax_collected"], 2)

    totals = {
        "tax_total": round(sum(p["tax_total"] for p in items), 2),
        "tax_collected": round(sum(p["tax_collected"] for p in items), 2),
        "tax_outstanding": round(sum(p["tax_total"] - p["tax_collected"] for p in items), 2),
        "tax_credited": round(sum(p["tax_credited"] for p in items), 2),
        "tax_refunded": round(sum(p["tax_refunded"] for p in items), 2),
        "gdx_tax": round(sum(p["gdx"]["tax_total"] for p in items), 2),
        "quickbooks_tax": round(sum(p["quickbooks"]["tax_total"] for p in items), 2),
    }
    return items, totals


@router.get("/sales-tax", response_model=None)
def sales_tax_report(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    period: str = Query(default="month", pattern="^(month|quarter)$"),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Sales tax collected, by period — plan §16 (Doug 2026-07-29: "we need to
    track sales tax and do a report on it").

    Splits GDX-generated invoices (INV-*) from QuickBooks imports (everything
    else) because they carry different provenance — the QB tax was computed in
    QuickBooks, the GDX tax by this app — and the office reconciles them
    separately. Also splits collected (paid) vs outstanding tax, since tax on
    an unpaid invoice isn't yet a remittance liability.

    Grouped on invoice_date (the tax point), not created_at. Counts ISSUED
    invoices only — status in (sent, paid, overdue). Drafts are excluded: a
    draft is a speculative invoice not yet handed to the customer, so its tax
    is not a liability and would otherwise inflate "outstanding" with amounts
    that may never be billed. Deposits (money before the work — no tax event)
    and voids are excluded too.

    The A2 question this report exists to ANSWER, not settle: whether GDX
    should be charging the 7.38% default at all on construction-contract work
    (see mn-sales-tax rule) is an accountant call — this shows the numbers.

    M18 (2026-08-24): credits and refunds DO net now — invoice_adjustments
    carries a pro-rata `tax_component` (Doug's ruling: amount × tax/total);
    a companion per-period query subtracts credits from the liability and
    refunds from liability AND collected, in the ADJUSTMENT's period (the
    GL's convention — a remitted month is never restated). Only rows whose
    parent is still in the liability universe net: a voided or deposit
    invoice's tax never counted, so its credits must not subtract.

    STILL OPEN — supersessions: a §12 replacement invoice
    (adjusts_invoice_id) double-counts against its original once such
    invoices exist (0 today). Gated on the §12 supersede model; reconcile
    there, not by patching this read path in isolation.
    """
    start_dt, end_dt, end_resolved = _resolve_date_range(start_date, end_date)
    trunc = "quarter" if period == "quarter" else "month"
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    date_trunc(:trunc, invoice_date) AS period_start,
                    CASE WHEN invoice_number LIKE 'INV-%' THEN 'gdx'
                         ELSE 'quickbooks' END AS source,
                    COUNT(*) AS invoice_count,
                    COALESCE(SUM(tax_amount), 0) AS tax_total,
                    -- M18: this keyed "collected" off `paid_at IS NOT NULL`.
                    -- A fully-credited invoice flips to paid with paid_at
                    -- stamped and ZERO cash received, so credited tax landed
                    -- in the remittance-liability bucket as if collected.
                    -- Six prod invoices carry paid_at with no payment at all;
                    -- two of them carry tax. Gate on real, non-voided cash.
                    COALESCE(SUM(CASE WHEN EXISTS (
                                          SELECT 1 FROM payments pm
                                          WHERE pm.invoice_id = invoices.id
                                            AND pm.voided_at IS NULL
                                      )
                                      THEN tax_amount ELSE 0 END), 0) AS tax_collected
                FROM invoices
                WHERE deleted_at IS NULL
                  AND status IN ('sent', 'paid', 'overdue')
                  AND (billing_type IS NULL OR billing_type != 'deposit')
                  AND invoice_date IS NOT NULL
                  AND invoice_date >= :start_dt
                  AND invoice_date < :end_dt
                GROUP BY 1, 2
                ORDER BY 1 ASC, 2 ASC
                """
            ),
            {"trunc": trunc, "start_dt": start_dt, "end_dt": end_dt},
        ).mappings().all()
    except Exception:
        log.exception("sales_tax_report_failed")
        raise HTTPException(status_code=500, detail="Unable to compute the sales-tax report") from None

    # M18: the tax given back, per the CREDIT's own period (created_at) —
    # a remitted month is never restated. Same window as the main query.
    try:
        adj_rows = db.execute(
            text(
                """
                SELECT
                    date_trunc(:trunc, a.created_at) AS period_start,
                    'adjustments' AS row_kind,
                    COALESCE(SUM(CASE WHEN a.kind IN ('credit_memo', 'credit_applied')
                                      THEN a.tax_component ELSE 0 END), 0) AS tax_credited,
                    COALESCE(SUM(CASE WHEN a.kind = 'refund'
                                      THEN a.tax_component ELSE 0 END), 0) AS tax_refunded
                FROM invoice_adjustments a
                JOIN invoices i ON i.id = a.invoice_id
                WHERE i.deleted_at IS NULL
                  -- Audit round 2 (the foundational hole): the parent must
                  -- still be IN the liability universe. A voided or deposit
                  -- invoice's tax never counts in tax_total, so netting its
                  -- credit would understate the liability forever (voiding
                  -- reverses the GL but the adjustment ROW survives).
                  AND i.status IN ('sent', 'paid', 'overdue')
                  AND (i.billing_type IS NULL OR i.billing_type != 'deposit')
                  AND a.tax_component > 0
                  AND a.created_at >= :start_dt
                  AND a.created_at < :end_dt
                GROUP BY 1
                """
            ),
            {"trunc": trunc, "start_dt": start_dt, "end_dt": end_dt},
        ).mappings().all()
    except Exception:
        log.exception("sales_tax_adjustments_failed")
        adj_rows = []

    items, totals = _rollup_sales_tax(list(rows) + list(adj_rows))
    return {
        "items": items,
        "period": period,
        "totals": totals,
        "range": {"start": start_dt, "end": end_dt, "end_date": end_resolved.isoformat()},
    }


@router.get("/daily-snapshot", response_model=None)
def daily_snapshot(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # Phase D audit 2026-04-27: when no params are given, the snapshot is
    # for *today only*, not the last 30 days. _resolve_date_range defaults
    # to a 30-day window, which made `new_jobs_today` return 165 because
    # the QB import landed 165 jobs 14 days ago — all "today". Force a
    # single-day window when the caller doesn't specify.
    if not start_date and not end_date:
        today = datetime.now(UTC).date()
        start_date = today.isoformat()
        end_date = today.isoformat()
    start_dt, end_dt, resolved_end = _resolve_date_range(start_date, end_date)
    today = resolved_end

    # Revenue billed in the window. Same semantics as /summary —
    # invoice_date with created_at fallback, billed-statuses only.
    _amount = Invoice.total
    _rev_date = _revenue_date_expr()
    _start_d = date.fromisoformat(start_dt[:10])
    _end_d = date.fromisoformat(end_dt[:10])
    today_revenue = db.scalar(
        select(func.coalesce(func.sum(_amount), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status.in_(_BILLED_STATUSES),
            _rev_date >= _start_d,
            _rev_date < _end_d,
        )
    ) or 0

    # Jobs completed in window — lifecycle_stage + completed_at fallback.
    _completed_at = func.coalesce(Job.completed_at, Job.created_at)
    jobs_completed_today = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            _completed_at >= start_dt,
            _completed_at < end_dt,
        )
    ) or 0

    # New jobs in window — created_at is correct here (this metric IS
    # about ingestion timing, not business semantics).
    new_jobs_today = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.created_at >= start_dt,
            Job.created_at < end_dt,
        )
    ) or 0

    # Assigned jobs today — UX audit F-27 / 2026-04-29. Mirrors what the
    # Dispatch board shows: jobs scheduled in the window AND assigned to a
    # technician AND still active (not completed/cancelled). The Dashboard
    # "Open Jobs" tile is the broader backlog; this tile answers
    # "what's actually on the board for today."
    assigned_jobs_today = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.scheduled_at.is_not(None),
            Job.scheduled_at >= start_dt,
            Job.scheduled_at < end_dt,
            Job.assigned_to.is_not(None),
            Job.assigned_to != "",
            Job.lifecycle_stage.in_(("scheduled", "in_progress")),
        )
    ) or 0

    # Open invoices — backlog count, not window-scoped. Same reasoning as
    # open_jobs in /summary: the user wants "right now, how many".
    open_inv_row = db.execute(
        select(
            func.count().label("open_invoices_count"),
            func.coalesce(func.sum(Invoice.balance_due), 0).label("open_invoices_total"),
        ).where(
            Invoice.deleted_at.is_(None),
            Invoice.balance_due > 0,
            Invoice.status.notin_(("paid", "void", "draft")),
        )
    ).mappings().first()

    # True overdue subset of the open backlog.
    overdue_row = db.execute(
        select(
            func.count().label("overdue_count"),
            func.coalesce(func.sum(Invoice.balance_due), 0).label("overdue_total"),
        ).where(
            Invoice.deleted_at.is_(None),
            Invoice.balance_due > 0,
            Invoice.status.notin_(("paid", "void", "draft")),
            Invoice.due_date.is_not(None),
            Invoice.due_date < today,
        )
    ).mappings().first()

    return {
        "today_revenue": float(today_revenue),
        "jobs_completed_today": int(jobs_completed_today),
        "new_jobs_today": int(new_jobs_today),
        "assigned_jobs_today": int(assigned_jobs_today),
        "open_invoices_count": int((open_inv_row or {}).get("open_invoices_count", 0) or 0),
        "open_invoices_total": float((open_inv_row or {}).get("open_invoices_total", 0) or 0),
        "overdue_invoices_count": int((overdue_row or {}).get("overdue_count", 0) or 0),
        "overdue_invoices_total": float((overdue_row or {}).get("overdue_total", 0) or 0),
        "snapshot_date": resolved_end.isoformat(),
    }


@router.get("/job-profitability", response_model=None)
def job_profitability(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    start_dt, end_dt, _ = _resolve_date_range(start_date, end_date)

    # Revenue per job via ORM — uses Invoice.total (canonical column); HAVING filters zero-revenue jobs
    _inv_revenue = func.coalesce(
        func.sum(Invoice.total), 0
    ).label("revenue")

    rows = db.execute(
        select(
            Job.id.label("job_id"),
            Job.title,
            Customer.name.label("customer_name"),
            _inv_revenue,
        )
        .outerjoin(Customer, Customer.id == Job.customer_id)
        .outerjoin(
            Invoice,
            (Invoice.job_id == Job.id)
            & Invoice.deleted_at.is_(None)
            & (Invoice.created_at >= start_dt)
            & (Invoice.created_at < end_dt),
        )
        .where(
            Job.deleted_at.is_(None),
            Job.created_at >= start_dt,
            Job.created_at < end_dt,
        )
        .group_by(Job.id, Job.title, Customer.name)
        .having(
            func.coalesce(func.sum(Invoice.total), 0) > 0
        )
        .order_by(
            func.coalesce(func.sum(Invoice.total), 0).desc()
        )
    ).mappings().all()

    return {
        "items": [
            {
                "job_id": str(r["job_id"]),
                "revenue": float(r.get("revenue") or 0),
                "labor_cost": 0.0,
                "overhead_cost": 0.0,
                "profit": float(r.get("revenue") or 0),
            }
            for r in rows
        ]
    }


@router.get("/technician-performance", response_model=None)
def technician_performance(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    start_dt, end_dt, _ = _resolve_date_range(start_date, end_date)

    rows = db.execute(
        text(
            """
            SELECT
                t.id AS technician_id,
                t.name AS technician_name,
                COUNT(DISTINCT CASE WHEN j.status = 'Complete' THEN j.id END) AS jobs_completed,
                COALESCE(SUM(ih.total), 0) AS revenue
            FROM technicians t
            LEFT JOIN jobs j
                ON (CAST(j.assigned_to AS TEXT) = CAST(t.id AS TEXT) OR CAST(j.assigned_to AS TEXT) = CAST(t.user_id AS TEXT))
               AND j.deleted_at IS NULL
               AND j.created_at >= :start_dt
               AND j.created_at < :end_dt
            LEFT JOIN invoices ih
                ON ih.job_id = j.id
               AND ih.deleted_at IS NULL
               AND ih.created_at >= :start_dt
               AND ih.created_at < :end_dt
            WHERE t.deleted_at IS NULL
            GROUP BY t.id, t.name
            HAVING COUNT(DISTINCT j.id) > 0
            ORDER BY revenue DESC
            """
        ),
        {"start_dt": start_dt, "end_dt": end_dt},
    ).mappings().all()

    return {
        "items": [
            {
                "technician_id": str(r["technician_id"]),
                "technician_name": r["technician_name"],
                "jobs_completed": int(r.get("jobs_completed") or 0),
                "revenue": float(r.get("revenue") or 0),
            }
            for r in rows
        ]
    }


@router.get("/revenue-analytics", response_model=None)
def revenue_analytics(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    start_dt, end_dt, _ = _resolve_date_range(start_date, end_date)

    # Revenue by date — text(); CAST(created_at AS DATE) is portable across SQLite + PostgreSQL
    by_period = db.execute(
        text(
            f"""
            SELECT
                {_revenue_date_sql()} AS period,
                COALESCE(SUM({_revenue_amount_sql()}), 0) AS revenue
            FROM invoices
            WHERE {_revenue_where_sql()}
              AND {_revenue_date_sql()} >= :start_d
              AND {_revenue_date_sql()} < :end_d
            GROUP BY {_revenue_date_sql()}
            ORDER BY period
            """  # noqa: S608 — interpolation is constants only; user input is bound
        ),
        {"start_d": date.fromisoformat(start_dt[:10]), "end_d": date.fromisoformat(end_dt[:10])},
    ).mappings().all()

    # Revenue by job type — ORM; uses Invoice.total (canonical) with fallback to total_amount
    _rev = func.coalesce(
        func.sum(Invoice.total), 0
    ).label("revenue")
    by_type = db.execute(
        select(
            func.coalesce(Job.job_type, "Unknown").label("job_type"),
            _rev,
        )
        .outerjoin(
            Invoice,
            (Invoice.job_id == Job.id)
            # M8: by_period and by_job_type ship in ONE payload. Before this,
            # by_period summed the null column ($0) while by_job_type coalesced
            # (real dollars), so the response's own total contradicted its own
            # detail rows. Both now share the one revenue definition.
            & sa_and(*_revenue_orm_filters())
            # M8: window on the billed date, matching by_period in this same
            # payload (and _summary_window). created_at is the import run.
            & (_revenue_date_expr() >= date.fromisoformat(start_dt[:10]))
            & (_revenue_date_expr() < date.fromisoformat(end_dt[:10])),
        )
        .where(
            Job.deleted_at.is_(None),
            Job.created_at >= start_dt,
            Job.created_at < end_dt,
        )
        .group_by(func.coalesce(Job.job_type, "Unknown"))
        .order_by(
            func.coalesce(func.sum(Invoice.total), 0).desc()
        )
    ).mappings().all()

    total_revenue = sum(float(r.get("revenue") or 0) for r in by_period)
    return {
        "total_revenue": total_revenue,
        "by_period": [
            {"period": r["period"], "revenue": float(r.get("revenue") or 0)}
            for r in by_period
        ],
        "by_job_type": [
            {"job_type": r["job_type"], "revenue": float(r.get("revenue") or 0)}
            for r in by_type
        ],
    }


@router.get("/customer-ltv", response_model=None)
def customer_ltv(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    start_dt, end_dt, _ = _resolve_date_range(start_date, end_date)

    # Customer lifetime value — ORM
    _ltv = func.coalesce(
        func.sum(Invoice.total), 0
    ).label("lifetime_value")

    rows = db.execute(
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            func.count(func.distinct(Job.id)).label("job_count"),
            _ltv,
        )
        .outerjoin(
            Job,
            (Job.customer_id == Customer.id)
            & Job.deleted_at.is_(None)
            & (Job.created_at >= start_dt)
            & (Job.created_at < end_dt),
        )
        .outerjoin(
            Invoice,
            (Invoice.job_id == Job.id)
            & Invoice.deleted_at.is_(None)
            & (Invoice.created_at >= start_dt)
            & (Invoice.created_at < end_dt),
        )
        .where(Customer.deleted_at.is_(None))
        .group_by(Customer.id, Customer.name)
        .having(func.count(func.distinct(Job.id)) > 0)
        .order_by(
            func.coalesce(func.sum(Invoice.total), 0).desc()
        )
    ).mappings().all()

    return {
        "items": [
            {
                "customer_id": str(r["customer_id"]),
                "customer_name": r["customer_name"],
                "job_count": int(r.get("job_count") or 0),
                "lifetime_value": float(r.get("lifetime_value") or 0),
            }
            for r in rows
        ]
    }


@router.get("/outstanding-aging", response_model=None)
def outstanding_aging(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Point-in-time AR backlog. Deliberately takes NO date range.

    M20 removed the `created_at` window; leaving `start_date`/`end_date` in the
    signature would have been worse than the bug — parameters a caller can pass
    that silently do nothing. Aging answers "what is owed right now", so there
    is nothing for a range to mean. (Zero frontend callers today; the endpoint
    is reachable but unrendered.)
    """
    resolved_end = datetime.now(UTC).date()

    # M20 (money-audit-2026-08-04): three defects in one query.
    #
    # 1. It windowed on `created_at`, so ANY receivable created more than 30
    #    days ago vanished from aging entirely — and the 91+ bucket could only
    #    ever fill from QB-imported rows whose created_at is the import date.
    #    Aging is a point-in-time backlog, not a windowed report; the date
    #    range is gone.
    # 2. No status filter, so drafts counted as receivables: a draft's
    #    balance_due equals its total at creation.
    # 3. It aged from `invoice_date` while /reports/cash-risk ages from
    #    `due_date` and excludes paid/void/draft — two AR agings that could not
    #    structurally agree. This now anchors on due_date to match, falling
    #    back to invoice_date only where due_date is null (legacy/imported).
    rows = db.execute(
        select(
            func.coalesce(Invoice.due_date, Invoice.invoice_date).label("age_from"),
            func.coalesce(Invoice.balance_due, 0).label("balance_due"),
        ).where(
            Invoice.deleted_at.is_(None),
            Invoice.balance_due > 0,
            # Same three terms /reports/cash-risk uses, so the two AR views can
            # finally agree. Before this they differed by $16,324.21 on prod:
            # aging admitted paid invoices and had no not-yet-due skip.
            Invoice.status.notin_(("paid", "void", "draft")),
        )
    ).mappings().all()

    counts = {"0_30": 0, "31_60": 0, "61_90": 0, "91_plus": 0}
    totals = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "91_plus": 0.0}

    for row in rows:
        inv_date = row.get("age_from")
        if inv_date is None:
            continue
        if isinstance(inv_date, str):
            try:
                inv_date = date.fromisoformat(inv_date[:10])
            except (ValueError, TypeError):
                log.exception("outstanding_aging_failed")
                continue
        elif isinstance(inv_date, datetime):
            inv_date = inv_date.date()
        age_days = (resolved_end - inv_date).days
        # Not yet due is not outstanding. cash-risk skips these; aging counted
        # them in the 0-30 bucket, which is most of why the two disagreed.
        if age_days < 0:
            continue
        if age_days <= 30:
            bucket = "0_30"
        elif age_days <= 60:
            bucket = "31_60"
        elif age_days <= 90:
            bucket = "61_90"
        else:
            bucket = "91_plus"
        counts[bucket] += 1
        totals[bucket] += float(row.get("balance_due") or 0)

    return {"counts": counts, "totals": totals}


# ── Sales funnel KPIs ────────────────────────────────────────────────────────
#
# "Sold" semantics: an estimate transitions to status='accepted' with
# accepted_at populated. That moment is when revenue is *booked*. Bookings
# lead invoicing by days/weeks (door installed later), so the funnel needs
# its own clock — separate from /summary's invoice-billed clock.

# EstimateLine.category is free text. Engine-priced lines write the bucket
# name directly ('doors'|'openers'|'parts'|'labor'|'other'). Free-form
# lines vary. We match exact + plural and exclude opener — it gets us the
# bookings-doors signal without false positives from "door opener".
_DOOR_CATEGORY_MATCH = func.lower(func.trim(EstimateLine.category)).in_(("door", "doors"))


def _sold_window(db: Session, start: datetime, end: datetime) -> dict:
    """Aggregate count + door_count + dollar_amount of estimates accepted
    in [start, end). Manual lines (no cost_snapshot) still contribute to
    dollar via Estimate.total — we trust the operator-set total."""
    rows = db.execute(
        select(Estimate.id, Estimate.total)
        .where(
            Estimate.deleted_at.is_(None),
            Estimate.status == "accepted",
            Estimate.accepted_at.is_not(None),
            Estimate.accepted_at >= start,
            Estimate.accepted_at < end,
        )
    ).all()
    count = len(rows)
    dollar_amount = sum(float(r.total or 0) for r in rows)
    if count == 0:
        return {"count": 0, "door_count": 0, "dollar_amount": 0.0, "avg_ticket": 0.0}
    estimate_ids = [r.id for r in rows]
    door_count = db.scalar(
        select(func.coalesce(func.sum(EstimateLine.quantity), 0))
        .where(EstimateLine.estimate_id.in_(estimate_ids), _DOOR_CATEGORY_MATCH)
    ) or 0
    return {
        "count": count,
        "door_count": int(door_count),
        "dollar_amount": dollar_amount,
        "avg_ticket": dollar_amount / count if count else 0.0,
    }


def _billed_window(db: Session, start_d: date, end_d: date) -> dict:
    """Sum invoice revenue (sent/paid/overdue) in [start_d, end_d) using
    coalesce(invoice_date, created_at::date) — same anchor as /summary."""
    _amount = Invoice.total
    _rev_date = _revenue_date_expr()
    row = db.execute(
        select(
            func.coalesce(func.sum(_amount), 0).label("revenue"),
            func.count().label("count"),
        ).where(
            Invoice.deleted_at.is_(None),
            Invoice.status.in_(_BILLED_STATUSES),
            _rev_date >= start_d,
            _rev_date < end_d,
        )
    ).mappings().first() or {}
    return {
        "revenue": float(row.get("revenue") or 0),
        "count": int(row.get("count") or 0),
    }


@router.get("/sales-funnel", response_model=None)
def sales_funnel(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Bookings KPIs across today / this-week / last 30 days plus close rate
    and outstanding-estimate aging."""
    now = datetime.now(UTC)
    today = now.date()
    today_start = datetime.combine(today, time.min, tzinfo=UTC)
    tomorrow_start = today_start + timedelta(days=1)
    # ISO week — Monday start.
    week_start = datetime.combine(today - timedelta(days=today.weekday()), time.min, tzinfo=UTC)
    last30_start = today_start - timedelta(days=29)

    sold_today = _sold_window(db, today_start, tomorrow_start)
    sold_week = _sold_window(db, week_start, tomorrow_start)
    sold_30d = _sold_window(db, last30_start, tomorrow_start)

    billed_today = _billed_window(db, today, today + timedelta(days=1))
    billed_week = _billed_window(db, week_start.date(), today + timedelta(days=1))

    # Close rate over last 30 days: accepted / (accepted+declined+expired)
    # filtered by sent_at — i.e., decisions on estimates that were actually
    # presented to customers. Drafts that never went out shouldn't dilute.
    # "rejected" is deliberately NOT a decision (2026-08-13): the bounce
    # detector sets it when the estimate EMAIL bounced — the customer never
    # saw the estimate, so counting it as a "no" would poison the rate.
    decision_total = db.scalar(
        select(func.count()).where(
            Estimate.deleted_at.is_(None),
            Estimate.sent_at.is_not(None),
            Estimate.sent_at >= last30_start,
            Estimate.sent_at < tomorrow_start,
            Estimate.status.in_(("accepted", "declined", "expired")),
        )
    ) or 0
    decision_accepted = db.scalar(
        select(func.count()).where(
            Estimate.deleted_at.is_(None),
            Estimate.sent_at.is_not(None),
            Estimate.sent_at >= last30_start,
            Estimate.sent_at < tomorrow_start,
            Estimate.status == "accepted",
        )
    ) or 0
    close_rate = (decision_accepted / decision_total) if decision_total else None

    # Outstanding estimates: sent but no terminal status. Aging from sent_at.
    outstanding_rows = db.execute(
        select(Estimate.id, Estimate.total, Estimate.sent_at)
        .where(
            Estimate.deleted_at.is_(None),
            Estimate.status == "sent",
            Estimate.sent_at.is_not(None),
        )
    ).all()
    buckets = {
        "lt_3d": {"label": "0-2 days", "count": 0, "dollar_amount": 0.0},
        "d3_7": {"label": "3-7 days", "count": 0, "dollar_amount": 0.0},
        "d8_14": {"label": "8-14 days", "count": 0, "dollar_amount": 0.0},
        "gt_14": {"label": "15+ days", "count": 0, "dollar_amount": 0.0},
    }
    out_total = 0.0
    for r in outstanding_rows:
        age_days = (now - r.sent_at).days
        amt = float(r.total or 0)
        out_total += amt
        if age_days < 3:
            key = "lt_3d"
        elif age_days <= 7:
            key = "d3_7"
        elif age_days <= 14:
            key = "d8_14"
        else:
            key = "gt_14"
        buckets[key]["count"] += 1
        buckets[key]["dollar_amount"] += amt

    return {
        "sold": {
            "today": sold_today,
            "this_week": sold_week,
            "last_30_days": sold_30d,
        },
        "billed": {
            "today": billed_today,
            "this_week": billed_week,
        },
        "close_rate": {
            "rate": close_rate,
            "accepted": int(decision_accepted),
            "decisions": int(decision_total),
            "window_days": 30,
        },
        "estimates_outstanding": {
            "count": len(outstanding_rows),
            "dollar_amount": out_total,
            "buckets": buckets,
        },
        "as_of": now.isoformat(),
    }


# ── Operations KPIs ──────────────────────────────────────────────────────────
#
# Several signals here are gated on data we don't yet capture cleanly:
# jobs.started_at + time_entries.clock_out are populated on <1% of rows
# (D35 deploy gate flagged both). Rather than emit fake numbers, return
# null with `unavailable_reason` so the UI can render an explanatory badge.

@router.get("/operations", response_model=None)
def operations_kpis(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    now = datetime.now(UTC)
    today = now.date()
    last30_start_dt = datetime.combine(today - timedelta(days=29), time.min, tzinfo=UTC)
    tomorrow_start_dt = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)

    _completed_at = func.coalesce(Job.completed_at, Job.created_at)

    # First-time fix rate: of jobs completed in last 30d, share with
    # is_return_visit=False.  parent_job_id+is_return_visit are written by
    # the followup-creation route; absent flag = first visit.
    completed_30d = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            _completed_at >= last30_start_dt,
            _completed_at < tomorrow_start_dt,
        )
    ) or 0
    callbacks_30d = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            Job.is_return_visit.is_(True),
            _completed_at >= last30_start_dt,
            _completed_at < tomorrow_start_dt,
        )
    ) or 0
    first_time_fix = (
        ((completed_30d - callbacks_30d) / completed_30d) if completed_30d else None
    )

    # Response speed: jobs created in last 30d that got a same-day or
    # next-day appointment (scheduled_at::date - created_at::date <= 1).
    booking_rows = db.execute(
        select(Job.created_at, Job.scheduled_at)
        .where(
            Job.deleted_at.is_(None),
            Job.created_at >= last30_start_dt,
            Job.created_at < tomorrow_start_dt,
            Job.scheduled_at.is_not(None),
        )
    ).all()
    same_day = 0
    next_day = 0
    for cr, sc in booking_rows:
        if not cr or not sc:
            continue
        delta_days = (sc.date() - cr.date()).days
        if delta_days <= 0:
            same_day += 1
        elif delta_days == 1:
            next_day += 1
    booking_total = len(booking_rows)
    same_day_rate = (same_day / booking_total) if booking_total else None
    next_day_rate = ((same_day + next_day) / booking_total) if booking_total else None

    # Avg job duration — powered by closeout ATTESTED hours (this tile
    # predates Phase 2 closeouts; its "data not yet captured" claim went
    # stale once they began capturing hours_worked). The closeout's
    # hours_worked is the tech's attested on-site duration — evidence,
    # unlike a created_at/completed_at delta, which would leak dispatch lag
    # into the number. Three exclusions, each load-bearing:
    #   * current rows only (supersede model keeps every restatement;
    #     averaging non-live rows double-counts a re-closed job);
    #   * hours_worked > 0 — zero is legal end-to-end (signature-only
    #     closeouts with require_hours_on_complete off, the default), and a
    #     zero row is "no duration attested", not "the job took 0 hours" —
    #     counting it dilutes an average labeled "attested";
    #   * jobs completed through legacy /complete paths never filed a
    #     closeout at all — jobs_measured tells the reader the sample size.
    duration_count, duration_sum = db.execute(
        select(func.count(), func.coalesce(func.sum(JobCloseout.hours_worked), 0)).where(
            JobCloseout.deleted_at.is_(None),
            JobCloseout.superseded_at.is_(None),
            JobCloseout.hours_worked > 0,
            JobCloseout.closed_at >= last30_start_dt,
            JobCloseout.closed_at < tomorrow_start_dt,
        )
    ).one()
    duration_count = int(duration_count or 0)
    avg_duration_hours = (
        round(float(duration_sum) / duration_count, 2) if duration_count else None
    )
    duration_unavailable = (
        None if duration_count else "no closeouts with attested hours in the window"
    )

    # Tech utilization — needs time_entries.clock_in + clock_out. clock_out
    # is empty (D35). Same treatment.
    util_unavailable = "time_entries.clock_out not yet captured by mobile clock-out flow"

    return {
        "first_time_fix": {
            "rate": first_time_fix,
            "completed": int(completed_30d),
            "callbacks": int(callbacks_30d),
            "window_days": 30,
        },
        "response_speed": {
            "same_day_rate": same_day_rate,
            "same_or_next_day_rate": next_day_rate,
            "same_day": int(same_day),
            "next_day": int(next_day),
            "total_booked": int(booking_total),
            "window_days": 30,
        },
        "avg_job_duration": {
            "value": avg_duration_hours,
            "unit": "hours",
            "jobs_measured": duration_count,
            "window_days": 30,
            "unavailable_reason": duration_unavailable,
        },
        "tech_utilization": {
            "value": None,
            "unavailable_reason": util_unavailable,
        },
        "as_of": now.isoformat(),
    }


# ── Cash & risk KPIs ─────────────────────────────────────────────────────────

@router.get("/cash-risk", response_model=None)
def cash_risk_kpis(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    now = datetime.now(UTC)
    today = now.date()
    last30_start_dt = datetime.combine(today - timedelta(days=29), time.min, tzinfo=UTC)
    tomorrow_start_dt = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)

    # AR aging: anchor on due_date, exclude paid/draft/void, exclude
    # invoices not yet due. Mirror the canonical reports.py semantics
    # rather than collections.py (which uses mixed-case status filters
    # and would miss most prod rows).
    invoices = db.execute(
        select(Invoice.id, Invoice.total, Invoice.balance_due, Invoice.due_date)
        .where(
            Invoice.deleted_at.is_(None),
            Invoice.status.notin_(("paid", "void", "draft")),
            Invoice.balance_due > 0,
            Invoice.due_date.is_not(None),
        )
    ).all()
    aging = {
        "current": {"label": "Current (0-30)", "count": 0, "total": 0.0},
        "d31_60": {"label": "31-60 days", "count": 0, "total": 0.0},
        "d61_90": {"label": "61-90 days", "count": 0, "total": 0.0},
        "d90_plus": {"label": "90+ days", "count": 0, "total": 0.0},
    }
    total_outstanding = 0.0
    for inv in invoices:
        days_overdue = (today - inv.due_date).days
        if days_overdue < 0:
            continue
        # M35: the fallback arm used to subtract `amount_paid`, a cache nothing
        # maintains. It was unreachable anyway — the query above already
        # filters `balance_due > 0` — so the stale column is simply gone.
        amount = float(inv.balance_due or 0)
        if amount <= 0:
            continue
        total_outstanding += amount
        if days_overdue <= 30:
            key = "current"
        elif days_overdue <= 60:
            key = "d31_60"
        elif days_overdue <= 90:
            key = "d61_90"
        else:
            key = "d90_plus"
        aging[key]["count"] += 1
        aging[key]["total"] += amount

    # Gross margin on bookings (last 30 days). Same math as the existing
    # /pricing pipeline endpoint: only engine-priced lines (cost_snapshot
    # + margin_pct_snapshot non-null) count toward cost — manual lines are
    # excluded from both sides. We also surface a manual-line flag so the
    # tile can warn.
    accepted = db.execute(
        select(Estimate.id)
        .where(
            Estimate.deleted_at.is_(None),
            Estimate.status == "accepted",
            Estimate.accepted_at.is_not(None),
            Estimate.accepted_at >= last30_start_dt,
            Estimate.accepted_at < tomorrow_start_dt,
        )
    ).scalars().all()
    total_sell = 0.0
    total_cost = 0.0
    estimates_with_manual = 0
    if accepted:
        line_rows = db.execute(
            select(EstimateLine.estimate_id, EstimateLine.unit_price, EstimateLine.quantity, EstimateLine.cost_snapshot, EstimateLine.margin_pct_snapshot)
            .where(EstimateLine.estimate_id.in_(accepted))
        ).all()
        seen_manual: set = set()
        for est_id, up, qty, cs, mps in line_rows:
            if cs is None or mps is None:
                seen_manual.add(est_id)
                continue
            qd = float(qty or 0)
            total_sell += float(up or 0) * qd
            total_cost += float(cs or 0) * qd
        estimates_with_manual = len(seen_manual)
    net_profit = total_sell - total_cost
    margin_pct = (net_profit / total_sell) if total_sell > 0 else None

    # D-S122b-invoice-margin-reports: backward-looking gross margin on
    # invoices billed in the same window. The "accepted estimates" block
    # above shows what tier-priced; this shows what actually got billed.
    # Same rules: lines without a cost_snapshot are excluded from both
    # sides; sent/paid invoices count, drafts don't.
    billed_invs = db.execute(
        select(Invoice.id)
        .where(
            Invoice.deleted_at.is_(None),
            Invoice.status.in_(["sent", "paid"]),
            Invoice.invoice_date.is_not(None),
            Invoice.invoice_date >= last30_start_dt.date(),
            Invoice.invoice_date < tomorrow_start_dt.date(),
        )
    ).scalars().all()
    inv_total_sell = 0.0
    inv_total_cost = 0.0
    inv_with_manual = 0
    if billed_invs:
        inv_line_rows = db.execute(
            select(
                InvoiceLine.invoice_id, InvoiceLine.unit_price,
                InvoiceLine.quantity, InvoiceLine.cost_snapshot,
            )
            .where(
                InvoiceLine.invoice_id.in_(billed_invs),
                InvoiceLine.deleted_at.is_(None),
            )
        ).all()
        inv_seen_manual: set = set()
        for inv_id, up, qty, cs in inv_line_rows:
            if cs is None:
                inv_seen_manual.add(inv_id)
                continue
            qd = float(qty or 0)
            inv_total_sell += float(up or 0) * qd
            inv_total_cost += float(cs or 0) * qd
        inv_with_manual = len(inv_seen_manual)
    inv_net_profit = inv_total_sell - inv_total_cost
    inv_margin_pct = (inv_net_profit / inv_total_sell) if inv_total_sell > 0 else None

    # Cash actually collected (last 30 days). The dashboard led with billed
    # revenue but never showed collected — the billed → collected →
    # outstanding story had a hole in the middle. Keyed on payment_date (the
    # receipt date the Record Payment dialog captures, backdatable), NOT
    # created_at, so a backfilled payment lands in the period the money
    # actually arrived. Voided payments stay history: excluded here exactly
    # as _recalculate_invoice and the ledger exclude them.
    # Refunds are asymmetric in this schema (adversarial audit catch): a FULL
    # Stripe refund VOIDS the payment row (already excluded above), but an
    # office refund is an append-only InvoiceAdjustment kind='refund' with
    # the Payment row left standing. Counting payments alone would make the
    # tile net-of-card-refunds but gross-of-check-refunds — subtract refund
    # adjustments in the same window so "collected" means NET CASH both ways.
    #
    # M3 (2026-08-23) added a third shape: a PARTIAL Stripe refund no longer
    # voids the payment (voiding $500 over a $50 goodwill refund was the bug),
    # and it is not auto-recorded as an adjustment either — keying refunds to
    # Stripe's refund id needs a column that does not exist yet, and without
    # one the auto-record double-books against a manual office entry. So until
    # the office records it, this tile OVERSTATES collected cash by the
    # refunded amount. The webhook writes a `stripe_partial_refund_received`
    # audit event so the gap is findable rather than silent.
    last30_start_date = today - timedelta(days=29)
    collected_count, collected_gross = db.execute(
        select(func.count(), func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.voided_at.is_(None),
            Payment.payment_date >= last30_start_date,
            Payment.payment_date <= today,
        )
    ).one()
    refunded_window = db.scalar(
        select(func.coalesce(func.sum(InvoiceAdjustment.amount), 0)).where(
            InvoiceAdjustment.kind == "refund",
            InvoiceAdjustment.created_at >= last30_start_dt,
            InvoiceAdjustment.created_at < tomorrow_start_dt,
        )
    ) or 0
    today_start_dt = datetime.combine(today, time.min, tzinfo=UTC)
    collected_today_gross = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.voided_at.is_(None),
            Payment.payment_date == today,
        )
    ) or 0
    refunded_today = db.scalar(
        select(func.coalesce(func.sum(InvoiceAdjustment.amount), 0)).where(
            InvoiceAdjustment.kind == "refund",
            InvoiceAdjustment.created_at >= today_start_dt,
            InvoiceAdjustment.created_at < tomorrow_start_dt,
        )
    ) or 0

    # Warranty callbacks — rate vs jobs completed in the same window.
    warranty_filed = db.scalar(
        select(func.count()).where(
            WarrantyClaim.deleted_at.is_(None),
            WarrantyClaim.filed_at.is_not(None),
            WarrantyClaim.filed_at >= last30_start_dt,
            WarrantyClaim.filed_at < tomorrow_start_dt,
        )
    ) or 0
    _completed_at = func.coalesce(Job.completed_at, Job.created_at)
    completed_window = db.scalar(
        select(func.count()).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            _completed_at >= last30_start_dt,
            _completed_at < tomorrow_start_dt,
        )
    ) or 0
    warranty_rate = (warranty_filed / completed_window) if completed_window else None

    return {
        "ar_aging": {
            "buckets": aging,
            "total_outstanding": total_outstanding,
        },
        "gross_margin": {
            "margin_pct": margin_pct,
            "total_sell": total_sell,
            "total_cost": total_cost,
            "net_profit": net_profit,
            "estimates_with_manual_lines": estimates_with_manual,
            "window_days": 30,
        },
        # D-S122b-invoice-margin-reports — backward-looking gross margin on
        # invoices billed in the same window. Companion to gross_margin above;
        # the dashboard can render either or both.
        "gross_margin_invoiced": {
            "margin_pct": inv_margin_pct,
            "total_sell": inv_total_sell,
            "total_cost": inv_total_cost,
            "net_profit": inv_net_profit,
            "invoices_with_manual_lines": inv_with_manual,
            "window_days": 30,
        },
        "warranty_callbacks": {
            "rate": warranty_rate,
            "filed": int(warranty_filed),
            "completed_jobs": int(completed_window),
            "window_days": 30,
        },
        "collected": {
            # NET cash: payments minus office-refund adjustments. A FULL
            # Stripe refund already voids its payment row, so those two paths
            # subtract exactly once. A PARTIAL Stripe refund is not subtracted
            # here until the office records it — see the M3 note above.
            "total": float(collected_gross or 0) - float(refunded_window or 0),
            "count": int(collected_count or 0),
            "refunded": float(refunded_window or 0),
            "today_total": float(collected_today_gross or 0) - float(refunded_today or 0),
            "window_days": 30,
        },
        "as_of": now.isoformat(),
    }


@router.get("/export")
def export_report(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    report_type: str = Query(default="jobs"),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    valid_report_types = ["jobs", "invoices", "customers", "revenue"]
    if report_type not in valid_report_types:
        raise HTTPException(status_code=400, detail="Invalid report type")

    start_dt, end_dt, _ = _resolve_date_range(start_date, end_date)
    params = {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "start_d": date.fromisoformat(start_dt[:10]),
        "end_d": date.fromisoformat(end_dt[:10]),
    }

    queries = {
        "jobs": "SELECT id, title, status, assigned_to, scheduled_at, created_at FROM jobs WHERE deleted_at IS NULL AND created_at >= :start_dt AND created_at < :end_dt",
        # M8: this exported `total_amount` verbatim — a column NULL on every
        # prod row — so the accountant's CSV carried a blank total on all 349
        # invoices. Export the coalesced amount under the name people expect.
        # Deliberately NOT filtered to billed statuses: this is the invoice
        # register, where a draft or void is a row you want to SEE.
        "invoices": f"SELECT id, invoice_number, status, {_revenue_amount_sql()} AS total, balance_due, due_date, {_revenue_date_sql()} AS invoice_date FROM invoices WHERE deleted_at IS NULL AND {_revenue_date_sql()} >= :start_d AND {_revenue_date_sql()} < :end_d",  # noqa: S608 — constants only; dates are bound
        "customers": "SELECT id, name, email, phone, created_at FROM customers WHERE deleted_at IS NULL AND created_at >= :start_dt AND created_at < :end_dt",
        "revenue": f"SELECT {_revenue_date_sql()} as date, COUNT(*) as invoice_count, COALESCE(SUM({_revenue_amount_sql()}),0) as revenue FROM invoices WHERE {_revenue_where_sql()} AND {_revenue_date_sql()} >= :start_d AND {_revenue_date_sql()} < :end_d GROUP BY {_revenue_date_sql()} ORDER BY 1",  # noqa: S608 — constants only; dates are bound
    }

    result = db.execute(text(queries[report_type]), params)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(result.keys())
    writer.writerows(result.fetchall())

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={report_type}_export.csv"},
    )
