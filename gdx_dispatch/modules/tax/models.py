"""Tax module ORM models. Tenant-plane.

Tables live in each tenant's own DB (per three-plane isolation rule —
isolation is the connection, no tenant_id filter columns).
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.models.tenant_models import TenantBase


def _utcnow() -> datetime:
    return datetime.utcnow()


class TaxConfig(TenantBase):
    """Single-row per tenant: the default tax rate the system applies when no
    customer-specific or jurisdiction-specific override is present.

    Future shape: this row stays the catch-all default; jurisdiction +
    customer overrides resolve through `resolve_rate(customer_id, address)`
    in service.py.
    """

    __tablename__ = "tax_config"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Default")
    # Stored as a decimal fraction (0.0738 = 7.38%). Use Numeric for accounting
    # precision — never Float. 5 digits + 4 fraction = max 9.9999 (999.99%),
    # which is absurd but cheap and safe.
    default_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0.0)
    # Most US jurisdictions do NOT tax labor (services). When False, lines
    # marked category == 'labor' are excluded from the taxable subtotal in
    # compute_estimate_totals. Defaults to False so a fresh tenant gets the
    # common-case behavior; flip on per-tenant for jurisdictions that tax
    # labor (parts of WV, HI, NM, SD, etc.).
    tax_labor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Show whether the operator has explicitly set this or it's defaulted.
    configured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TaxExemption(TenantBase):
    """Customer-level exemption. When exempt=True, all of this customer's
    invoices skip tax regardless of what TaxConfig says.

    A `certificate_id` field captures the reseller / non-profit
    certificate the customer presented. `exempt_until` lets the rate
    auto-expire — many states require exemption certificates be
    renewed annually.
    """

    __tablename__ = "tax_exemption"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    exempt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    certificate_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exempt_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    exempt_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# Phase-2 placeholder: jurisdictional rate lookup. Not yet wired into
# resolve_rate; documented here so the schema migration is a no-op when
# the second-phase code lands.
#
# class TaxJurisdiction(TenantBase):
#     __tablename__ = "tax_jurisdiction"
#     id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
#     zip_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
#     state: Mapped[str] = mapped_column(String(2), nullable=False)
#     city: Mapped[str | None] = mapped_column(String(100), nullable=True)
#     county: Mapped[str | None] = mapped_column(String(100), nullable=True)
#     rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
#     effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
#     effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
