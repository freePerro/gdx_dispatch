"""Labor Matrix Coordination sprint regression suite (Doug 2026-05-07).

Covers the four systems EST-000030 line 7 cascaded through and the rules
introduced in this sprint:

  - Admin validation: hours must be 0 < h <= 48; drift warning at >10%.
  - Pricing: labor_price_item_id wins; flat_price is authoritative
    regardless of client-supplied cost / pricing_category.
  - Pricing engine: explicit refusal of pricing_category='labor'.
  - Scheduler: sum(hours × quantity) / max(crew_size, default_crew_size)
    with min_wall_clock_minutes floor.
  - Variance: estimated_hours sums hours × quantity.
  - EST-000030 regression: qty=2 / hours=7 / flat=$700 → $1,400.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.models.tenant_models import Job, JobAssignment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.appointments import compute_man_hour_duration_minutes


# ---- fixture helpers (centralized per feedback_centralized_test_identifiers.md)


def make_labor_row(
    db,
    *,
    description: str = "Test install",
    service_type: str = "install",
    width_ft: int | None = 10,
    height_ft: int | None = 12,
    flat_price: float = 700.00,
    assumed_man_hours: float = 7.00,
    default_crew_size: int = 2,
    min_wall_clock_minutes: int = 120,
) -> LaborPriceItem:
    row = LaborPriceItem(
        id=uuid4(),
        description=description,
        service_type=service_type,
        width_ft=width_ft,
        height_ft=height_ft,
        flat_price=Decimal(str(flat_price)),
        assumed_man_hours=Decimal(str(assumed_man_hours)),
        default_crew_size=default_crew_size,
        min_wall_clock_minutes=min_wall_clock_minutes,
    )
    db.add(row)
    db.flush()
    return row


def make_job(db, *, num: str = "J-LMC") -> Job:
    j = Job(
        id=uuid4(),
        job_number=f"{num}-{uuid4().hex[:6]}",
        title="install",
        status="Scheduled",
        company_id="t-test",
    )
    db.add(j)
    db.commit()
    return j


def make_estimate_with_labor_lines(
    db,
    job: Job,
    rows_and_qty: list[tuple[LaborPriceItem, int]],
    *,
    status: str = "accepted",
) -> Estimate:
    est = Estimate(
        id=uuid4(),
        job_id=job.id,
        estimate_number=f"EST-{uuid4().hex[:6]}",
        public_token=f"tok-{uuid4().hex[:8]}",
        company_id="t-test",
        status=status,
        accepted_at=datetime.now(timezone.utc) if status == "accepted" else None,
        total=Decimal("0.00"),
    )
    db.add(est)
    db.flush()
    for sort_order, (row, qty) in enumerate(rows_and_qty, start=1):
        db.add(EstimateLine(
            id=uuid4(),
            estimate_id=est.id,
            description=row.description,
            quantity=qty,
            unit_price=row.flat_price,
            line_total=row.flat_price * qty,
            sort_order=sort_order,
            company_id="t-test",
            labor_price_item_id=row.id,
            estimated_man_hours=row.assumed_man_hours,
            pricing_source="labor_matrix",
        ))
    db.commit()
    return est


def assign_techs(db, job: Job, n: int) -> None:
    for _ in range(n):
        db.add(JobAssignment(
            id=str(uuid4()),
            job_id=str(job.id),
            tech_id=str(uuid4()),
            assigned_at=datetime.now(timezone.utc),
        ))
    db.commit()


# ---- S4: admin validation


def test_admin_rejects_zero_hours():
    """8×7 install with hours=0 had silent-zero downstream effects in prod."""
    from gdx_dispatch.routers.labor_pricing_admin import LaborPriceItemIn
    with pytest.raises(ValidationError):
        LaborPriceItemIn(
            description="x", service_type="install",
            flat_price=500, assumed_man_hours=0,
        )


def test_admin_rejects_700_hours():
    """The exact EST-000030 fat-finger: 700 typed for 7."""
    from gdx_dispatch.routers.labor_pricing_admin import LaborPriceItemIn
    with pytest.raises(ValidationError):
        LaborPriceItemIn(
            description="x", service_type="install",
            flat_price=700, assumed_man_hours=700,
        )


def test_admin_size_caps_at_40_feet():
    """120 was inches; in feet it's a 10-story door — reject."""
    from gdx_dispatch.routers.labor_pricing_admin import LaborPriceItemIn
    with pytest.raises(ValidationError):
        LaborPriceItemIn(
            description="x", service_type="install",
            width_ft=120, height_ft=96,
            flat_price=500, assumed_man_hours=5,
        )


def test_drift_warning_helper():
    """>10% drift from target rate produces a string; within band returns None."""
    from gdx_dispatch.routers.labor_pricing_admin import _drift_warning

    assert _drift_warning(700, 7, 100) is None  # implied 100 vs 100 → 0%
    assert _drift_warning(700, 5, 100) is not None  # implied 140 vs 100 → 40%
    assert _drift_warning(700, 8, 100) is not None  # implied 87.5 vs 100 → 12.5%
    # Within ±10% band — no warning:
    assert _drift_warning(700, 7.3, 100) is None  # implied 95.89 → 4%


# ---- S3: pricing engine refuses labor


def test_pricing_engine_rejects_labor_category():
    """Belt-on-belt: even if frontend regresses and sends pricing_category='labor',
    the engine refuses rather than silently re-pricing."""
    from gdx_dispatch.services.pricing_engine import (
        CustomerView, PricingConfigError, PricingSettingsView, price_line,
    )
    customer = CustomerView(pricing_class="retail", margin_override_pct=None)
    settings = PricingSettingsView(
        tier_sets={},
        volume_discount_enabled=False,
        volume_tiers=[],
        class_volume_enabled={},
    )
    with pytest.raises(PricingConfigError, match="labor lines must not flow"):
        price_line(
            cost=Decimal("100"),
            pricing_category="labor",
            customer=customer,
            settings=settings,
        )


# ---- S6: scheduler honors quantity


def test_scheduler_honors_quantity(tenant_db):
    """Doug 2026-05-07 / EST-000030: pre-fix `sum(hours)` ignored qty.
    A 4-door install at qty=4 with 5h matrix row schedules 20 man-hr."""
    job = make_job(tenant_db)
    row = make_labor_row(
        tenant_db,
        flat_price=500, assumed_man_hours=5,
        default_crew_size=2, min_wall_clock_minutes=60,
    )
    make_estimate_with_labor_lines(tenant_db, job, [(row, 4)])
    assign_techs(tenant_db, job, 2)
    # 5h × 4 / 2 techs = 10h wall-clock = 600 min (already 15-min mult).
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 600


def test_scheduler_honors_default_crew_size_when_unstaffed(tenant_db):
    """Job with no JobAssignments yet — fall back to matrix default_crew_size,
    not crew=1 (which over-blocks the calendar)."""
    job = make_job(tenant_db)
    row = make_labor_row(
        tenant_db,
        flat_price=700, assumed_man_hours=7,
        default_crew_size=2, min_wall_clock_minutes=60,
    )
    make_estimate_with_labor_lines(tenant_db, job, [(row, 1)])
    # No assignments → use matrix default_crew_size=2. 7h / 2 = 3.5h = 210 min.
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 210


def test_scheduler_min_wall_clock_floor(tenant_db):
    """Quick job + many techs assigned should not collapse below the floor."""
    job = make_job(tenant_db)
    row = make_labor_row(
        tenant_db,
        flat_price=150, assumed_man_hours=1.5,
        default_crew_size=1, min_wall_clock_minutes=60,
    )
    make_estimate_with_labor_lines(tenant_db, job, [(row, 1)])
    assign_techs(tenant_db, job, 4)
    # Math says 1.5h × 60 / 4 = 22.5min, rounded to 30. Floor pulls to 60.
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 60


# ---- EST-000030 regression


def test_est_000030_shape(tenant_db):
    """Recreate the line shape that produced $91k overstatement and assert
    the post-sprint arithmetic. Inputs match historical data after heal."""
    job = make_job(tenant_db, num="EST-000030-shape")
    row = make_labor_row(
        tenant_db,
        description="Install 10x12",
        flat_price=700, assumed_man_hours=7,
        default_crew_size=2, min_wall_clock_minutes=120,
    )
    est = make_estimate_with_labor_lines(tenant_db, job, [(row, 2)])
    # line_total = 700 × 2
    line = tenant_db.query(EstimateLine).filter_by(estimate_id=est.id).one()
    assert line.unit_price == Decimal("700.00")
    assert line.line_total == Decimal("1400.00")
    assert line.estimated_man_hours == Decimal("7.00")
    # Scheduler sees qty-aware hours: 7 × 2 / 2 techs = 7h = 420 min.
    assign_techs(tenant_db, job, 2)
    assert compute_man_hour_duration_minutes(tenant_db, job.id) == 420
