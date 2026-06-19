"""Labor pricing matrix — tenant-plane models (Sprint S97).

Tenant-configurable flat-rate labor pricing. Each row carries a flat price
and an *assumed man-hours* budget. The man-hours number is the contract
that downstream systems consume:

    - Estimating:     displays flat_price; stamps estimated_man_hours onto
                      the EstimateLine (slice 4).
    - Scheduling:     wall_clock = assumed_man_hours / crew_size, where
                      crew_size = count of active JobAssignment rows on
                      the job (slice 7).
    - Job costing:    actual_man_hours = sum of timeclock entries on the
                      job; cost reads PayrollEntry first (truth) then
                      Technician.hourly_rate (fallback) — see PayrollEntry
                      docstring. Variance = actual − assumed (slice 8).

Row identity is dual-shape per Doug 2026-05-04:
    - Size-keyed rows:   (service_type, width_in, height_in)  e.g. 10x8 install
    - SKU/desc-keyed:    (service_type, sku)                  for tenants whose
                         pricing axis isn't size (spring length, opener HP).

Door removal is a separate row (service_type='removal'), not a modifier.
Travel is folded into flat_price.

Per CLAUDE.md three-plane isolation: tenant-plane = db-per-tenant, isolation
is the connection itself. NO tenant_id / company_id columns.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class LaborPriceItem(TenantBase):
    """One row in the tenant's labor pricing matrix."""

    __tablename__ = "labor_price_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    # Row identity — at least one of (sku) or (width_in & height_in) should be
    # populated, but we don't enforce at the column level: tenants may key by
    # description alone for one-off rows. Quote-engine resolution order is
    # size → sku → manual.
    sku: Mapped[str] = mapped_column(String(40), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    service_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # 2026-05-07 — door size in FEET. Earlier rows mixed feet/inches in the
    # same columns (108x84 vs 10x8). Renamed to width_ft/height_ft and units
    # locked to feet via _validate_size_pair (≤ 40ft is the realistic cap).
    width_ft: Mapped[int] = mapped_column("width_ft", Integer, nullable=True)
    height_ft: Mapped[int] = mapped_column("height_ft", Integer, nullable=True)

    # Pricing + ops budget. Both independent inputs — flat_price is market-
    # driven; assumed_man_hours is what we expect a crew to take. The implied
    # rate (price / hours) is computed on the fly for "is this row upside
    # down?" UI hints, never stored.
    flat_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    assumed_man_hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=0)

    # 2026-05-07 — scheduling fields. assumed_man_hours is total man-hours
    # (5h = 5h for 1 tech or 2.5h for 2 techs). default_crew_size is the row's
    # natural staffing (residential opener: 1; commercial sectional: 2; large
    # commercial: 2 + helper). min_wall_clock_minutes prevents the duration
    # math from suggesting absurdly-short blocks when many techs are assigned
    # to a quick job. Both feed appointments.compute_man_hour_duration_minutes.
    default_crew_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    min_wall_clock_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default="15"
    )

    notes: Mapped[str] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Versioning — so old estimates' snapshots can be cross-referenced even
    # after the row is superseded. MVP behavior is edit-in-place; effective_*
    # exists for slice-8 reporting and for the supersede pattern later.
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    effective_to: Mapped[date] = mapped_column(Date, nullable=True)

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
