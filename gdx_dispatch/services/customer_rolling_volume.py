"""Customer rolling-12mo paid-volume — compute + cache writer (Sprint 1.0.6).

The pricing engine consumes a customer's trailing-365-day paid invoice
volume to look up the loyalty discount tier. Computing it on every
estimate-create would scale poorly on tenants with large invoice
histories, so we denormalize onto the customer row and refresh on
write events (payment.received) plus a stale-read check at
estimate-create.

Pure function module — no router, no FastAPI imports. Caller passes a
tenant-scoped session.

Cash-basis semantics (Doug 2026-04-25):
- Sum of `Payment.amount` joined to `Invoice` where the payment landed
  in the trailing 365 days, the invoice belongs to this customer, and
  the invoice is not voided.
- Strictly cash collected — protects margin from customers with big
  estimates that never close.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Customer, Invoice, Payment

ROLLING_WINDOW_DAYS = 365
STALE_REFRESH_AFTER = timedelta(hours=1)


def _now_utc(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def compute_paid_volume(
    customer_id: UUID,
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> Decimal:
    """Live SUM — sum of payments against this customer's non-void invoices
    where payment_date is within the trailing window.

    Returns Decimal('0') if there are no qualifying payments. Pure read,
    no side effects.
    """
    cutoff = _now_utc(now).date() - timedelta(days=ROLLING_WINDOW_DAYS)
    total = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .where(Invoice.customer_id == customer_id)
        .where(Invoice.status != "void")
        .where(Payment.payment_date >= cutoff)
    ).scalar_one()
    # SUM may return int 0 (from coalesce default) or Decimal — normalize.
    return Decimal(str(total)) if not isinstance(total, Decimal) else total


def refresh_cached_volume(
    customer_id: UUID,
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> Decimal:
    """Recompute volume + write back to Customer row. Returns the new value.

    Caller is responsible for committing the session. We flush so the
    write is visible inside the same transaction (e.g. when called from
    record_payment before the outer commit).
    """
    value = compute_paid_volume(customer_id, db, now=now)
    timestamp = _now_utc(now)
    customer = db.get(Customer, customer_id)
    if customer is None:
        # Customer disappeared mid-flight — don't blow up the caller.
        return value
    customer.cached_rolling_volume_paid_12mo = value
    customer.cached_rolling_volume_at = timestamp
    db.flush()
    return value


def is_cache_stale(
    cached_at: Optional[datetime], *, now: Optional[datetime] = None
) -> bool:
    """True if the cache is missing or older than STALE_REFRESH_AFTER."""
    if cached_at is None:
        return True
    cur = _now_utc(now)
    # Naive timestamps in cached_at (legacy rows) — treat as UTC for the
    # comparison rather than crashing.
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    return (cur - cached_at) > STALE_REFRESH_AFTER


def get_or_refresh(
    customer_id: UUID,
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> Decimal:
    """Return cached value if fresh, else recompute. Used by estimate-create.

    Returns Decimal('0') if the customer has no cache and refresh fails to
    find them. Caller commits.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        return Decimal("0")
    if not is_cache_stale(customer.cached_rolling_volume_at, now=now):
        return Decimal(customer.cached_rolling_volume_paid_12mo or 0)
    return refresh_cached_volume(customer_id, db, now=now)
