"""Reseller models.

A contractor or wholesale customer resells our estimate to their own customer,
under their own brand, at a markup they choose. Both tables are PRIVATE to
that customer: only the portal router (`routers/portal_resale.py`) reads them,
and `tests/test_reseller_privacy.py` fails if a staff surface starts to. The
markup never reaches our invoices, ledger or AR — a resale quote is a document
the contractor hands on, not a sale we make.

Tenant plane — db-per-tenant, no `company_id` columns (isolation is the
connection). Plain UUID columns, not ForeignKeys: the door_listings rule for
pointers into tables other modules own.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class ResellerProfile(TenantBase):
    """The contractor's brand. One per customer: the brand belongs to the
    company, not to one portal login."""

    __tablename__ = "reseller_profiles"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)

    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    website: Mapped[str | None] = mapped_column(String(200), nullable=True)
    license_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: A minted basename (`core/branding_logo.RESELLER_LOGO_RE`), never a path.
    logo_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    terms_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_markup_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0"), server_default="0"
    )

    #: `service.DISCLAIMER_VERSION` when it was agreed to; a reworded
    #: disclaimer has a new version, so the old acceptance stops counting.
    disclaimer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    disclaimer_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The portal `CustomerUser` who agreed.
    disclaimer_accepted_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResaleQuote(TenantBase):
    """One branded quote, frozen when it is made: editing the estimate later
    does not move a number the contractor already quoted or is tracking."""

    __tablename__ = "resale_quotes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    estimate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    #: The portal `CustomerUser` who made it.
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    reference: Mapped[str] = mapped_column(String(60), nullable=False)
    end_customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    end_customer_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    markup_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    #: Our price to them: pre-tax, after discount. NULL on an open
    #: Good/Better/Best proposal, which has one price per option
    #: (`lines_snapshot["options"]`) and no single total.
    base_subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    resale_subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    #: What the PDF prints, already marked up (`service.build_snapshot`).
    lines_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    hide_line_prices: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
