"""Sprint 1.0.6 — customer rolling-12mo paid-volume tests.

Covers:
- compute_paid_volume: trailing-365 sum semantics, void-invoice exclusion,
  no-payment customer returns 0.
- refresh_cached_volume: writes back to Customer row + sets timestamp.
- is_cache_stale: missing/stale → True; fresh → False.
- get_or_refresh: hot path returns cached; cold/stale path recomputes.
- End-to-end: payment lands → cache updates → engine sees new tier.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from gdx_dispatch.models.pricing_engine import (
    CustomerVolumeDiscountTier,
    PricingSettings,
    seed_default_pricing,
)
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job, Payment
from gdx_dispatch.services.customer_rolling_volume import (
    ROLLING_WINDOW_DAYS,
    STALE_REFRESH_AFTER,
    compute_paid_volume,
    get_or_refresh,
    is_cache_stale,
    refresh_cached_volume,
)
from gdx_dispatch.services.pricing_engine import (
    CustomerView,
    EstimateLineInput,
    hydrate_settings_from_db,
    price_estimate,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

COMPANY_ID = "test-company"


def _make_customer(db, name: str = "Acme Co", pricing_class: str | None = None) -> Customer:
    cust = Customer(
        id=uuid4(),
        name=name,
        company_id=COMPANY_ID,
        pricing_class=pricing_class,
    )
    db.add(cust)
    db.flush()
    return cust


def _make_paid_invoice(
    db,
    customer: Customer,
    amount: Decimal,
    *,
    payment_date: date,
    status: str = "paid",
) -> Invoice:
    """Create a Job + Invoice + Payment in one shot."""
    job = Job(
        id=uuid4(),
        customer_id=customer.id,
        title="J",
        company_id=COMPANY_ID,
    )
    db.add(job)
    db.flush()
    inv = Invoice(
        id=uuid4(),
        job_id=job.id,
        customer_id=customer.id,
        company_id=COMPANY_ID,
        invoice_number=f"INV-{uuid4().hex[:8]}",
        public_token=uuid4().hex,
        subtotal=amount,
        total=amount,
        balance_due=Decimal("0"),
        status=status,
    )
    db.add(inv)
    db.flush()
    pay = Payment(
        id=uuid4(),
        invoice_id=inv.id,
        company_id=COMPANY_ID,
        amount=amount,
        method="cash",
        payment_date=payment_date,
    )
    db.add(pay)
    db.flush()
    return inv


# ---------------------------------------------------------------------------
# compute_paid_volume — pure read
# ---------------------------------------------------------------------------


def test_compute_paid_volume_no_payments_returns_zero(tenant_db):
    cust = _make_customer(tenant_db)
    assert compute_paid_volume(cust.id, tenant_db) == Decimal("0")


def test_compute_paid_volume_sums_payments_inside_window(tenant_db):
    cust = _make_customer(tenant_db)
    today = date.today()
    _make_paid_invoice(tenant_db, cust, Decimal("250.00"), payment_date=today - timedelta(days=10))
    _make_paid_invoice(tenant_db, cust, Decimal("750.50"), payment_date=today - timedelta(days=200))
    total = compute_paid_volume(cust.id, tenant_db)
    assert total == Decimal("1000.50")


def test_compute_paid_volume_excludes_payments_outside_window(tenant_db):
    cust = _make_customer(tenant_db)
    today = date.today()
    # Inside: 100 (10d ago)
    _make_paid_invoice(tenant_db, cust, Decimal("100"), payment_date=today - timedelta(days=10))
    # Outside: 999 (400d ago) — must NOT count
    _make_paid_invoice(tenant_db, cust, Decimal("999"), payment_date=today - timedelta(days=400))
    assert compute_paid_volume(cust.id, tenant_db) == Decimal("100")


def test_compute_paid_volume_excludes_voided_invoices(tenant_db):
    """Voided invoices' payments must NOT inflate rolling volume — the
    money may have been refunded, and it definitely doesn't represent a
    closed sale Doug wants to reward."""
    cust = _make_customer(tenant_db)
    today = date.today()
    _make_paid_invoice(tenant_db, cust, Decimal("100"), payment_date=today - timedelta(days=5))
    _make_paid_invoice(
        tenant_db, cust, Decimal("500"),
        payment_date=today - timedelta(days=5),
        status="void",
    )
    assert compute_paid_volume(cust.id, tenant_db) == Decimal("100")


def test_compute_paid_volume_excludes_other_customers(tenant_db):
    cust_a = _make_customer(tenant_db, name="A")
    cust_b = _make_customer(tenant_db, name="B")
    today = date.today()
    _make_paid_invoice(tenant_db, cust_a, Decimal("100"), payment_date=today - timedelta(days=5))
    _make_paid_invoice(tenant_db, cust_b, Decimal("200"), payment_date=today - timedelta(days=5))
    assert compute_paid_volume(cust_a.id, tenant_db) == Decimal("100")
    assert compute_paid_volume(cust_b.id, tenant_db) == Decimal("200")


def test_compute_paid_volume_window_boundary_inclusive_low(tenant_db):
    """Payment exactly at cutoff day (now - 365) is INCLUDED (>= cutoff).

    Pin `now` to a fixed UTC moment so the boundary semantics aren't subject
    to UTC-vs-local-date drift on the test runner.
    """
    cust = _make_customer(tenant_db)
    pinned_now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    cutoff = pinned_now.date() - timedelta(days=ROLLING_WINDOW_DAYS)

    # Exactly 365d ago — must count
    _make_paid_invoice(tenant_db, cust, Decimal("100"), payment_date=cutoff)
    assert compute_paid_volume(cust.id, tenant_db, now=pinned_now) == Decimal("100")
    # 366d ago — must NOT count
    _make_paid_invoice(
        tenant_db, cust, Decimal("999"),
        payment_date=cutoff - timedelta(days=1),
    )
    assert compute_paid_volume(cust.id, tenant_db, now=pinned_now) == Decimal("100")


# ---------------------------------------------------------------------------
# refresh_cached_volume — write-back
# ---------------------------------------------------------------------------


def test_refresh_cached_volume_writes_value_and_timestamp(tenant_db):
    cust = _make_customer(tenant_db)
    today = date.today()
    _make_paid_invoice(tenant_db, cust, Decimal("250"), payment_date=today - timedelta(days=10))

    before = datetime.now(timezone.utc)
    value = refresh_cached_volume(cust.id, tenant_db)
    after = datetime.now(timezone.utc)

    assert value == Decimal("250")
    tenant_db.refresh(cust)
    assert cust.cached_rolling_volume_paid_12mo == Decimal("250")
    assert cust.cached_rolling_volume_at is not None
    cached_at = cust.cached_rolling_volume_at
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    assert before <= cached_at <= after


def test_refresh_cached_volume_missing_customer_doesnt_crash(tenant_db):
    """Customer disappeared mid-flight (race) — return computed value (0)."""
    fake_id = uuid4()
    assert refresh_cached_volume(fake_id, tenant_db) == Decimal("0")


# ---------------------------------------------------------------------------
# is_cache_stale
# ---------------------------------------------------------------------------


def test_is_cache_stale_when_none():
    assert is_cache_stale(None) is True


def test_is_cache_stale_when_fresh():
    fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert is_cache_stale(fresh) is False


def test_is_cache_stale_when_old():
    old = datetime.now(timezone.utc) - (STALE_REFRESH_AFTER + timedelta(minutes=1))
    assert is_cache_stale(old) is True


def test_is_cache_stale_handles_naive_timestamp():
    """Legacy rows with tzinfo-naive timestamps must not crash the check."""
    naive_old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
    assert is_cache_stale(naive_old) is True


# ---------------------------------------------------------------------------
# get_or_refresh
# ---------------------------------------------------------------------------


def test_get_or_refresh_uses_cache_when_fresh(tenant_db):
    cust = _make_customer(tenant_db)
    cust.cached_rolling_volume_paid_12mo = Decimal("999")
    cust.cached_rolling_volume_at = datetime.now(timezone.utc)
    tenant_db.flush()

    # No actual payments — cache says 999, that's what we get
    assert get_or_refresh(cust.id, tenant_db) == Decimal("999")


def test_get_or_refresh_recomputes_when_stale(tenant_db):
    cust = _make_customer(tenant_db)
    today = date.today()
    _make_paid_invoice(tenant_db, cust, Decimal("450"), payment_date=today - timedelta(days=5))

    # Force stale cache with a wrong value
    cust.cached_rolling_volume_paid_12mo = Decimal("1.00")
    cust.cached_rolling_volume_at = datetime.now(timezone.utc) - timedelta(hours=2)
    tenant_db.flush()

    value = get_or_refresh(cust.id, tenant_db)
    assert value == Decimal("450")  # recomputed from real data


def test_get_or_refresh_missing_customer_returns_zero(tenant_db):
    assert get_or_refresh(uuid4(), tenant_db) == Decimal("0")


# ---------------------------------------------------------------------------
# End-to-end: payment refresh → engine picks up new tier
# ---------------------------------------------------------------------------


def _seed_volume_tier(db, settings: PricingSettings, vmin, vmax, pct) -> None:
    db.add(CustomerVolumeDiscountTier(
        settings_id=settings.id,
        volume_min_12mo=Decimal(str(vmin)),
        volume_max_12mo=Decimal(str(vmax)) if vmax is not None else None,
        discount_pct=Decimal(str(pct)),
        sort_order=0,
    ))
    db.flush()


def test_end_to_end_payment_lands_then_engine_sees_new_discount(tenant_db):
    """Customer pays a $200k invoice → cache refreshes → engine applies 4% tier."""
    seed_default_pricing(tenant_db)
    settings = tenant_db.query(PricingSettings).one()
    settings.volume_discount_enabled = True
    _seed_volume_tier(tenant_db, settings, 100_000, None, 0.04)
    tenant_db.commit()

    cust = _make_customer(tenant_db, pricing_class="retail")
    today = date.today()

    # Pre-payment: cache empty → no discount
    refresh_cached_volume(cust.id, tenant_db)
    tenant_db.commit()
    pricing_view = hydrate_settings_from_db(tenant_db)
    customer_view = CustomerView(
        pricing_class="retail",
        margin_override_pct=None,
        cached_rolling_volume=Decimal(cust.cached_rolling_volume_paid_12mo or 0),
    )
    lines = [EstimateLineInput(cost=Decimal("200"), pricing_category="doors", quantity=Decimal("1"))]
    pre = price_estimate(lines, customer_view, pricing_view)
    assert pre.volume_discount_pct == Decimal("0")

    # Payment lands ($200k)
    _make_paid_invoice(tenant_db, cust, Decimal("200000"), payment_date=today)
    refresh_cached_volume(cust.id, tenant_db)
    tenant_db.commit()
    tenant_db.refresh(cust)

    customer_view = CustomerView(
        pricing_class="retail",
        margin_override_pct=None,
        cached_rolling_volume=Decimal(cust.cached_rolling_volume_paid_12mo or 0),
    )
    post = price_estimate(lines, customer_view, pricing_view)
    assert post.volume_discount_pct == Decimal("0.04")
    assert post.volume_discount_amount > Decimal("0")
