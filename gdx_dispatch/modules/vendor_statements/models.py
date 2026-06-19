"""Vendor statement models — slice 1.

A `VendorStatement` is a parsed PDF statement of account. Each line on the
statement becomes a `VendorStatementLine`. Slice 1 captures the data; slices
2+ add classification (job vs inventory), job matching, and reconciliation
against GDX invoices.

Tenant plane — db-per-tenant, no `tenant_id` columns.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class VendorStatement(TenantBase):
    """One uploaded vendor statement PDF.

    Slice 1: `status` is always 'parsed'. Slice 2 adds 'review' (some lines
    classified) and 'reconciled' (every job-line tied to a customer invoice).
    """
    __tablename__ = "vendor_statements"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    vendor_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id"), nullable=True, index=True
    )

    parser_name: Mapped[str] = mapped_column(String(60), nullable=False)
    parser_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    raw_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="parsed")

    uploaded_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list["VendorStatementLine"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        order_by="VendorStatementLine.line_no",
    )


class VendorStatementLine(TenantBase):
    __tablename__ = "vendor_statement_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    statement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendor_statements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    vendor_invoice_no: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    vendor_job_no: Mapped[str | None] = mapped_column(String(60), nullable=True)
    line_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))

    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    po_ref: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    aging_bucket: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Slice 2+ fills these. Slice 1 leaves NULL.
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    matched_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_aging_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    statement: Mapped[VendorStatement] = relationship(back_populates="lines")
