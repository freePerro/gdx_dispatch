"""Vendor invoice (A/P bill) intake models.

A ``VendorInvoice`` is a parsed supplier bill (e.g. a Midwest Wholesale Doors
retail-sale invoice). Each line becomes a ``VendorInvoiceLine`` which the
office routes — via a human-confirmed review queue — to a job (cost + billing
checklist), to stock (inventory receipt), or to overhead.

Design: docs/design/vendor-invoice-intake-plan.md (DRAFT v4, 3 audit rounds).
This is Phase 1: models + parser + dedup + match/confirm services + router.
Full A/P accrual is out of scope (GL Phase 1 owns it); these rows are its
future source data.

Tenant plane — db-per-tenant, no ``tenant_id`` columns. Mirrors the sibling
``vendor_statements`` module.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

# Header status
STATUS_OPEN = "open"
STATUS_PAID = "paid"
STATUS_VOID = "void"
VALID_STATUSES = {STATUS_OPEN, STATUS_PAID, STATUS_VOID}

# Line disposition (how a line is routed on confirm)
DISP_PENDING = "pending"
DISP_JOB = "job"
DISP_STOCK = "stock"
DISP_OVERHEAD = "overhead"
DISP_SKIP = "skip"
VALID_DISPOSITIONS = {DISP_PENDING, DISP_JOB, DISP_STOCK, DISP_OVERHEAD, DISP_SKIP}

# Line kind — item vs the synthetic freight/tax lines the parser materializes
# so shipping and tax dollars are routable (and never leak between payables and
# costing).
KIND_ITEM = "item"
KIND_FREIGHT = "freight"
KIND_TAX = "tax"
VALID_KINDS = {KIND_ITEM, KIND_FREIGHT, KIND_TAX}

# Line confirmation status
LINE_PENDING = "pending"
LINE_CONFIRMED = "confirmed"


class VendorInvoice(TenantBase):
    __tablename__ = "vendor_invoices"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    vendor_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id"), nullable=True, index=True
    )
    # The vendor name exactly as it appeared on the bill (or as the LLM read
    # it). Resolved to ``vendor_id`` via the vendors table + name_aliases before
    # the (vendor, invoice_number) dedup check. Kept raw for audit.
    vendor_name_raw: Mapped[str] = mapped_column(String(200), nullable=False)

    invoice_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The vendor's PO# text — on Midwest bills this is a human note carrying the
    # customer / job reference (a name, not a system key). The match queue uses
    # it as the primary job-matching signal.
    po_reference: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    terms: Mapped[str | None] = mapped_column(String(60), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    shipping: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_OPEN)
    # Header-level job suggestion the office confirmed (or set manually). Lines
    # may still route individually.
    matched_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True
    )

    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id"), nullable=True, index=True
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False, default="upload")  # upload | email
    extraction_method: Mapped[str] = mapped_column(String(20), nullable=False, default="parser")  # parser | llm | manual

    # Dedup layer 3 (advisory, NOT a control — see design §3a / [AUDIT-R3]): set
    # when another open invoice from the same vendor has the same total within a
    # short window. Points at the other invoice; a human decides.
    possible_duplicate_of_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendor_invoices.id"), nullable=True
    )

    uploaded_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[VendorInvoiceLine]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="VendorInvoiceLine.line_no",
        foreign_keys="VendorInvoiceLine.vendor_invoice_id",
    )


class VendorInvoiceLine(TenantBase):
    __tablename__ = "vendor_invoice_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    vendor_invoice_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendor_invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL for synthetic freight/tax lines the parser materializes.
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default=KIND_ITEM)

    # The vendor's generic item label (e.g. "Garage Door Material").
    item_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The real, matchable description (Notes text on Midwest bills).
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("1"))
    # What WE were charged (a cost). 4dp because the vendor prints unit prices
    # to 4 places.
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))

    disposition: Mapped[str] = mapped_column(String(20), nullable=False, default=DISP_PENDING)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Routing targets (set on confirm). job_id/inventory_item_id/expense_id are
    # FKs to Base-metadata tables (created first in create_all, same pattern as
    # the sibling statement module). job_part_needed_id is a plain String(36) —
    # JobPartNeeded uses string PKs, no cross-type FK.
    job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True
    )
    inventory_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inventory_items.id"), nullable=True
    )
    expense_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("expenses.id"), nullable=True
    )
    job_part_needed_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    stock_adjustment_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LINE_PENDING)
    confirmed_by_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    invoice: Mapped[VendorInvoice] = relationship(
        back_populates="lines", foreign_keys=[vendor_invoice_id]
    )
