"""Revenue projection aggregator.

Inputs (per-tenant):
  - Open invoices: status in ('sent', 'overdue'), balance_due > 0.
    Bucketed by age relative to due_date (fall back to sent_at, then
    created_at). Each bucket multiplied by the tenant's collection-rate
    setting for that bucket.
  - Scheduled jobs: lifecycle_stage in ('scheduled', 'estimate'),
    billing_status = 'unbilled', scheduled_at within window. Estimated
    value = SUM of latest Estimate.total per job (joined). Multiplied
    by `scheduled_realization_rate`.
  - Recurring (optional): qb_recurring_transactions where active=true
    and next_date in window; counted at face amount (no probability,
    since QBO recurring schedules are deterministic).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Invoice, Job
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.forecasting.models import (
    CADENCE_ANNUAL,
    CADENCE_BIWEEKLY,
    CADENCE_MONTHLY,
    CADENCE_QUARTERLY,
    CADENCE_SEMIANNUAL,
    CADENCE_WEEKLY,
    DEFAULT_COLLECT_0_30,
    DEFAULT_COLLECT_31_60,
    DEFAULT_COLLECT_61_90,
    DEFAULT_COLLECT_90_PLUS,
    DEFAULT_SCHEDULED_REALIZATION,
    DEFAULT_WINDOW_DAYS,
    STREAM_STATUS_ACTIVE,
    ForecastSettings,
    QBRecurringTransaction,
    RecurringStream,
)

# Cadence-aware step. Calendar-month variants use dateutil.relativedelta so a
# monthly stream anchored on the 25th lands on the 25th every month
# (timedelta(days=30) drifts to the 24th in month 2, 23rd in month 3, etc.).
def _advance_cursor(cursor: date, cadence: str) -> date | None:
    if cadence == CADENCE_WEEKLY:
        return cursor + timedelta(days=7)
    if cadence == CADENCE_BIWEEKLY:
        return cursor + timedelta(days=14)
    if cadence == CADENCE_MONTHLY:
        return cursor + relativedelta(months=1)
    if cadence == CADENCE_QUARTERLY:
        return cursor + relativedelta(months=3)
    if cadence == CADENCE_SEMIANNUAL:
        return cursor + relativedelta(months=6)
    if cadence == CADENCE_ANNUAL:
        return cursor + relativedelta(years=1)
    return None


# Step-back helper used to advance from last_observed_date when next_expected is null.
def _step_for(cadence: str) -> relativedelta | timedelta | None:
    if cadence == CADENCE_WEEKLY: return timedelta(days=7)  # noqa: E701
    if cadence == CADENCE_BIWEEKLY: return timedelta(days=14)  # noqa: E701
    if cadence == CADENCE_MONTHLY: return relativedelta(months=1)  # noqa: E701
    if cadence == CADENCE_QUARTERLY: return relativedelta(months=3)  # noqa: E701
    if cadence == CADENCE_SEMIANNUAL: return relativedelta(months=6)  # noqa: E701
    if cadence == CADENCE_ANNUAL: return relativedelta(years=1)  # noqa: E701
    return None


def get_or_create_settings(db: Session) -> ForecastSettings:
    row = db.execute(select(ForecastSettings)).scalar_one_or_none()
    if row is None:
        row = ForecastSettings()
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _settings_dict(s: ForecastSettings) -> dict[str, Any]:
    return {
        "default_window_days": int(s.default_window_days),
        "collect_rate_0_30": float(s.collect_rate_0_30),
        "collect_rate_31_60": float(s.collect_rate_31_60),
        "collect_rate_61_90": float(s.collect_rate_61_90),
        "collect_rate_90_plus": float(s.collect_rate_90_plus),
        "scheduled_realization_rate": float(s.scheduled_realization_rate),
        "include_recurring": bool(s.include_recurring),
    }


_DECIMAL_FIELDS = (
    "default_window_days",
    "collect_rate_0_30",
    "collect_rate_31_60",
    "collect_rate_61_90",
    "collect_rate_90_plus",
    "scheduled_realization_rate",
)


def update_settings(db: Session, payload: dict[str, Any]) -> ForecastSettings:
    s = get_or_create_settings(db)
    for f in (*_DECIMAL_FIELDS, "include_recurring"):
        if f in payload and payload[f] is not None:
            value = payload[f]
            # Numeric columns expect Decimal under Postgres; floats from
            # Pydantic land cleanly on SQLite but can DataError on PG.
            if f in _DECIMAL_FIELDS and not isinstance(value, Decimal):
                value = Decimal(str(value))
            setattr(s, f, value)
    s.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(s)
    return s


def _ar_aging_bucket(age_days: int) -> str:
    if age_days <= 30:
        return "0_30"
    if age_days <= 60:
        return "31_60"
    if age_days <= 90:
        return "61_90"
    return "90_plus"


def _bucket_rate(settings: ForecastSettings, bucket: str) -> float:
    return {
        "0_30": float(settings.collect_rate_0_30),
        "31_60": float(settings.collect_rate_31_60),
        "61_90": float(settings.collect_rate_61_90),
        "90_plus": float(settings.collect_rate_90_plus),
    }[bucket]


def _ar_reference_date(inv: Invoice) -> date:
    if inv.due_date:
        return inv.due_date
    if inv.sent_at:
        return inv.sent_at.date()
    if getattr(inv, "created_at", None):
        return inv.created_at.date()
    return date.today()


def _open_ar_projection(db: Session, settings: ForecastSettings, today: date, window_days: int) -> dict[str, Any]:
    """Expected collection from open AR.

    Stage B: each bucket's rate is the *calibrated within-window* rate measured
    by the Stage-A loop when enough evidence exists (calibrated_window_rates),
    otherwise the configured rate as a prior. `rate_source` records which was
    used per bucket so the forecast is transparent about what it has learned vs.
    what is still a default. With no reconciled snapshots every bucket falls back
    to the configured rate, so behaviour is unchanged until calibration kicks in.
    """
    from gdx_dispatch.modules.forecasting.calibration import calibrated_window_rates

    invoices = db.execute(
        select(Invoice).where(
            Invoice.status.in_(("sent", "overdue")),
            Invoice.balance_due > 0,
        )
    ).scalars().all()

    calibrated = calibrated_window_rates(db, window_days, today)

    def _rate_for(bucket: str) -> tuple[float, str]:
        info = calibrated.get(bucket) or {}
        if info.get("calibrated") and info.get("rate") is not None:
            return float(info["rate"]), "calibrated"
        return _bucket_rate(settings, bucket), "configured"

    buckets = {}
    for b in ("0_30", "31_60", "61_90", "90_plus"):
        rate_used, rate_source = _rate_for(b)
        buckets[b] = {
            "open_total": 0.0, "expected_total": 0.0, "invoice_count": 0,
            "rate_used": rate_used, "rate_source": rate_source,
        }
    for inv in invoices:
        ref = _ar_reference_date(inv)
        age = (today - ref).days
        bucket = _ar_aging_bucket(max(0, age))
        amount = float(inv.balance_due or 0)
        rate = buckets[bucket]["rate_used"]
        buckets[bucket]["open_total"] += amount
        buckets[bucket]["expected_total"] += amount * rate
        buckets[bucket]["invoice_count"] += 1
    return {
        "open_total": sum(b["open_total"] for b in buckets.values()),
        "expected_total": sum(b["expected_total"] for b in buckets.values()),
        "uses_calibration": any(b["rate_source"] == "calibrated" for b in buckets.values()),
        "by_bucket": buckets,
    }


def _scheduled_jobs_projection(db: Session, settings: ForecastSettings, today: date, window_days: int) -> dict[str, Any]:
    window_end = today + timedelta(days=window_days)
    jobs_q = (
        select(Job)
        .where(
            Job.scheduled_at.is_not(None),
            Job.scheduled_at >= datetime.combine(today, datetime.min.time()),
            Job.scheduled_at <= datetime.combine(window_end, datetime.max.time()),
            Job.lifecycle_stage.in_(("scheduled", "estimate", "service_call")),
            Job.billing_status.in_(("unbilled",)),
            Job.deleted_at.is_(None),
        )
    )
    jobs = db.execute(jobs_q).scalars().all()

    estimated_value_by_job: dict[Any, float] = {}
    if jobs:
        ids = [j.id for j in jobs]
        rows = db.execute(
            select(Estimate.job_id, func.sum(Estimate.total)).where(Estimate.job_id.in_(ids)).group_by(Estimate.job_id)
        ).all()
        for jid, total in rows:
            estimated_value_by_job[jid] = float(total or 0)

    rate = float(settings.scheduled_realization_rate)
    scheduled_total = 0.0
    expected_total = 0.0
    by_job: list[dict[str, Any]] = []
    for j in jobs:
        val = estimated_value_by_job.get(j.id, 0.0)
        scheduled_total += val
        expected_total += val * rate
        by_job.append({
            "job_id": str(j.id),
            "job_number": j.job_number,
            "title": j.title,
            "scheduled_at": j.scheduled_at.isoformat() if j.scheduled_at else None,
            "estimated_value": val,
        })
    return {
        "job_count": len(jobs),
        "scheduled_total": scheduled_total,
        "expected_total": expected_total,
        "jobs": by_job,
    }


def _qbo_template_projection(db: Session, today: date, window_days: int) -> dict[str, Any]:
    """QBO RecurringTransaction templates that are scheduled to fire in window.

    These are *aspirational* — a template that the user set up in QBO. They
    do not reflect what actually clears the bank. Counterpart is
    _observed_stream_projection which projects from real bank activity.
    """
    window_end = today + timedelta(days=window_days)
    rows = db.execute(
        select(QBRecurringTransaction).where(
            QBRecurringTransaction.active.is_(True),
            QBRecurringTransaction.next_date.is_not(None),
            QBRecurringTransaction.next_date >= today,
            QBRecurringTransaction.next_date <= window_end,
        )
    ).scalars().all()
    total = sum(float(r.amount or 0) for r in rows)
    return {
        "count": len(rows),
        "expected_total": total,
        "items": [
            {
                "qb_id": r.qb_id,
                "name": r.name,
                "txn_type": r.txn_type,
                "customer_name": r.customer_name,
                "amount": float(r.amount or 0),
                "next_date": r.next_date.isoformat() if r.next_date else None,
                "interval_type": r.interval_type,
            }
            for r in rows
        ],
    }


def _observed_stream_projection(db: Session, today: date, window_days: int) -> dict[str, Any]:
    """Project active RecurringStream rows forward through the window.

    For each active stream, walk next_expected_date forward by cadence step
    until we exit the window. Skip past term limits (term_total_occurrences
    reached or term_end_date passed). Median amount = (amount_min + amount_max)/2.

    These are *observed* — every stream is grounded in real bank-clear data.
    Dedup against QBO templates happens in the caller (_combined_recurring).
    """
    window_end = today + timedelta(days=window_days)
    streams = db.execute(
        select(RecurringStream).where(
            RecurringStream.status == STREAM_STATUS_ACTIVE,
            RecurringStream.deleted_at.is_(None),
        )
    ).scalars().all()

    items: list[dict[str, Any]] = []
    total = 0.0
    count = 0
    for s in streams:
        step = _step_for(s.cadence)
        if step is None:
            continue
        # Start from next_expected_date if known; otherwise advance from
        # last_observed_date by one cadence step; otherwise today.
        cursor = s.next_expected_date
        if cursor is None and s.last_observed_date is not None:
            cursor = s.last_observed_date + step
        if cursor is None:
            cursor = today
        # Skip already-past projections
        if cursor < today:
            cursor = today

        median_amount = (float(s.amount_min) + float(s.amount_max)) / 2.0
        occurrences_remaining = None
        if s.term_total_occurrences is not None:
            occurrences_remaining = max(0, int(s.term_total_occurrences) - int(s.occurrences_seen))

        projected_dates: list[date] = []
        while cursor <= window_end:
            # Term gates
            if s.term_end_date is not None and cursor > s.term_end_date:
                break
            if occurrences_remaining is not None and len(projected_dates) >= occurrences_remaining:
                break
            projected_dates.append(cursor)
            nxt = _advance_cursor(cursor, s.cadence)
            if nxt is None or nxt == cursor:
                break
            cursor = nxt

        for d in projected_dates:
            items.append({
                "stream_id": str(s.id),
                "label": s.label,
                "payee_pattern": s.payee_pattern,
                "source": s.source,
                "cadence": s.cadence,
                "amount": median_amount,
                "next_date": d.isoformat(),
            })
            total += median_amount
            count += 1
    return {"count": count, "expected_total": total, "items": items}


def _combined_recurring(qbo: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Merge QBO-template and observed-stream projections with conservative dedup.

    Both sources are returned in ``sources`` so the UI can show each on its
    own. The merged ``items`` list drops a QBO template only when ALL three
    conditions match an observed stream:
      1. Amount is within ±15% of the observed stream's projected amount.
      2. QBO template's ``name`` / ``customer_name`` (free-text the user
         typed in QBO) contains the observed stream's normalized
         payee_pattern as a substring, case-insensitive — OR vice versa.
      3. Same calendar month (YYYY-MM prefix of next_date).

    AUDITOR-FLAGGED LIMITATION (kept transparent rather than silently failing):
    The observed.payee_pattern is a normalized bank-feed token (e.g.
    "PHONE.COM") while QBO's template ``name`` is a free-text user label
    (e.g. "Phone Service - Monthly"). Substring match catches the common
    case where the user puts the merchant name in the QBO label, but
    misses cases like "monthly comm svc" vs "PHONE.COM" — those will
    double-count until the user manually ends one side. ``qbo_overridden``
    tells the UI how many were actually deduped so the user can see when
    dedup is silent.
    """
    items: list[dict[str, Any]] = list(observed["items"])
    qbo_overridden = 0
    qbo_kept: list[dict[str, Any]] = []

    for q in qbo["items"]:
        q_name = ((q.get("name") or q.get("customer_name") or "")).upper()
        q_amt = float(q.get("amount") or 0)
        q_month = (q.get("next_date") or "")[:7]
        matched = False
        for obs in observed["items"]:
            obs_payee = (obs.get("payee_pattern") or "").upper()
            obs_amt = float(obs.get("amount") or 0)
            obs_month = (obs.get("next_date") or "")[:7]
            if obs_month != q_month:
                continue
            if obs_amt == 0:
                continue
            if abs(q_amt - obs_amt) / obs_amt > 0.15:
                continue
            if not obs_payee or not q_name:
                continue
            if obs_payee in q_name or q_name in obs_payee:
                matched = True
                break
        if matched:
            qbo_overridden += 1
        else:
            qbo_kept.append({**q, "source": "qbo_template"})

    items.extend(qbo_kept)
    total = sum(it["amount"] for it in items)
    return {
        "count": len(items),
        "expected_total": total,
        "items": items,
        "qbo_overridden": qbo_overridden,
        "sources": {
            "qbo_templates": qbo,
            "observed": observed,
        },
    }


def revenue_projection(db: Session, window_days: int | None = None, today: date | None = None) -> dict[str, Any]:
    settings = get_or_create_settings(db)
    if window_days is None:
        window_days = int(settings.default_window_days)
    if today is None:
        today = date.today()

    ar = _open_ar_projection(db, settings, today, window_days)
    scheduled = _scheduled_jobs_projection(db, settings, today, window_days)
    if settings.include_recurring:
        qbo = _qbo_template_projection(db, today, window_days)
        observed = _observed_stream_projection(db, today, window_days)
        recurring = _combined_recurring(qbo, observed)
    else:
        recurring = {
            "count": 0,
            "expected_total": 0.0,
            "items": [],
            "qbo_overridden": 0,
            "sources": {
                "qbo_templates": {"count": 0, "expected_total": 0.0, "items": []},
                "observed": {"count": 0, "expected_total": 0.0, "items": []},
            },
        }

    expected_total = ar["expected_total"] + scheduled["expected_total"] + recurring["expected_total"]
    return {
        "window_days": window_days,
        "as_of": today.isoformat(),
        "expected_total": expected_total,
        "open_ar": ar,
        "scheduled_jobs": scheduled,
        "recurring": recurring,
        "settings": _settings_dict(settings),
    }
